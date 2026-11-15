# AWS Infrastructure Installer

boto3를 사용하여 AWS 인프라 리소스를 생성하는 Python 스크립트입니다.  
CDK 스택과 동등한 AWS 인프라를 프로그래밍 방식으로 배포합니다.

## 목차

1. [개요](#개요)
2. [설정값](#설정값)
3. [생성되는 리소스](#생성되는-리소스)
4. [주요 함수](#주요-함수)
5. [실행 방법](#실행-방법)
6. [배포 순서](#배포-순서)
7. [AgentCore Runtime installer (별도)](#agentcore-runtime-installer-별도)

---

## 개요

이 스크립트는 **strands-runtime** 프로젝트의 Web UI(ECS)와 Bedrock Knowledge Base 등 공통 AWS 인프라를 자동으로 생성합니다.

- **Web UI**: ECS Fargate (`application/` — FastAPI + 프론트엔드) — 사용자 입력·MCP/Skill 선택·결과 표시
- **Strands Agent**: AgentCore Runtime (`runtime_agent/strands/installer.py`) — 추론·MCP·Skill 실행
- MCP 서버는 Runtime 컨테이너 **내부**에서 기동됩니다.
- **S3 Files**: AgentCore 세션·ECS app-data 영속화를 위해 S3 버킷을 NFS로 마운트

### 주요 특징
- **완전 자동화**: 단일 스크립트로 ECS·RAG·네트워킹 인프라 배포
- **멱등성**: 이미 존재하는 리소스는 재사용
- **에러 핸들링**: 각 단계별 예외 처리, 실패 시에도 `application/config.json`에 부분 정보 저장
- **로깅**: 상세한 배포 진행 상황 출력
- **S3 Vectors 기반 RAG**: Bedrock Knowledge Base가 OpenSearch Serverless 대신 S3 Vectors를 벡터 스토어로 사용
- **S3 Files 세션 스토리지**: AgentCore `/mnt/workspace` + ECS `/mnt/app-data` 영속 마운트
- **ECS Fargate 배포**: multi-stage Dockerfile 이미지를 ECR에 push한 뒤 ECS Fargate(ARM64) 서비스로 실행
- **AgentCore 연동**: Web Search Gateway 생성 및 Strands Agent Runtime 자동 배포
- **SSE 장시간 스트림**: ALB idle timeout·CloudFront origin read timeout을 120초로 설정
- **CloudFront→ALB 오리진 보호**: Secrets Manager 랜덤 헤더 주입 + ALB default 403 (헤더 일치 시에만 forward)
- **KB IAM 전파 대기**: Knowledge Base 역할 Trust Policy·inline policy 확인 후 assume 재시도

### 사전 요구사항
- **ARM64 빌드 호스트**: ECS/AgentCore 이미지는 `linux/arm64` 네이티브 빌드만 지원 (예: t4g, m7g EC2). x86 호스트에서는 QEMU 크로스 빌드 없이 즉시 실패합니다.
- **Docker CLI + buildx**: ARM64 호스트에서 컨테이너 이미지 빌드 및 ECR push (`docker buildx build --push`)
- **디스크 여유**: Docker 빌드 전 최소 약 2GB 여유 공간 확인 (`DOCKER_MIN_FREE_MB`)
- **AWS CLI**: ECR 로그인 (`aws ecr get-login-password`)
- **boto3** 및 스크립트 실행에 필요한 AWS 자격 증명 (S3 Files API용 `s3files` 클라이언트 포함)
- **IAM 권한**: S3, IAM, VPC, ECS, ECR, CloudFront, Bedrock Agent, S3 Vectors, AgentCore Control, **S3 Files**
  - Knowledge Base 생성 시 `iam:PassRole` (Knowledge Base 서비스 역할에 대해)
  - AgentCore Runtime 배포 시 `runtime_agent/strands/installer.py` 추가 권한

---

## 설정값

```python
# 기본 설정 (installer.py 상단)
project_name = "strands-runtime"   # 프로젝트 이름 (최소 3자)
region = "us-west-2"               # AWS 리전
git_name = "strands-runtime"       # Git 저장소 이름 (레거시 EC2 SSM 배포용)

# AgentCore Web Search Gateway
AGENTCORE_GATEWAY_REGION = "us-west-2"
AGENTCORE_WEBSEARCH_GATEWAY_NAME = "gateway-websearch"
AGENTCORE_WEBSEARCH_TARGET_NAME = "websearch"

# SSE / ALB 타임아웃 (장시간 tool run 대비)
SSE_ORIGIN_READ_TIMEOUT_SECONDS = 120
ALB_IDLE_TIMEOUT_SECONDS = 120

# 자동 생성되는 변수
account_id = sts_client.get_caller_identity()["Account"]
bucket_name = f"storage-for-{project_name}-{account_id}-{region}"
vector_bucket_name = f"{project_name}-{account_id}"
vector_index_name = project_name

# 벡터 인덱스 설정
embedding_dimensions = 1024
embedding_data_type = "float32"
distance_metric = "cosine"

# Bedrock Knowledge Base 필수 메타데이터 (S3 Vectors non-filterable)
BEDROCK_NON_FILTERABLE_METADATA_KEYS = [
    "AMAZON_BEDROCK_TEXT",
    "AMAZON_BEDROCK_METADATA",
]

# S3 Files (Runtime session + ECS app-data)
S3_FILES_SESSION_PREFIX = "agentcore-sessions/"
S3_FILES_APP_DATA_PREFIX = "app-data/"
APP_DATA_MOUNT_PATH = "/mnt/app-data"
SESSION_STORAGE_MOUNT_PATH = "/mnt/workspace"

# AgentCore Runtime 이름: project_name의 '-' → '_' (예: strands_runtime)
# agent_runtime_name(project_name)

# CloudFront→ALB 오리진 검증 헤더
custom_header_name = "X-Custom-Header"
# 값은 소스에 두지 않음. Secrets Manager:
#   {project_name}/cloudfront-alb-origin-header
# get_or_create_alb_origin_header()가 최초 배포 시 랜덤 생성·이후 재사용
ALB_ORIGIN_HEADER_SECRET_NAME = f"{project_name}/cloudfront-alb-origin-header"
```

---

## 생성되는 리소스

### 1. S3 버킷
- **이름**: `storage-for-{project_name}-{account_id}-{region}`
- **설정**:
  - CORS 활성화 (GET, POST, PUT)
  - 퍼블릭 액세스 차단
  - 버전 관리 **Enabled** (S3 Files file system 생성 필수; 신규 bucket은 `create_s3_bucket`에서, 기존 bucket은 `create_s3_files_session_storage` 시 자동 활성화)
  - `docs/` 폴더 자동 생성
  - S3 Files 세션 prefix: `agentcore-sessions/`

### 2. IAM 역할

| 역할 | 설명 |
|------|------|
| `role-knowledge-base-for-{project_name}-{region}` | Bedrock Knowledge Base용 역할 (프로젝트 S3 Get/List, AOSS collection, Bedrock Invoke) |
| `role-ecs-task-for-{project_name}-{region}` | ECS 태스크용 역할 (Bedrock Invoke/Mantle/KB ingest, AgentCore invoke, 프로젝트 S3, S3 Files mount) |
| `role-ecs-execution-for-{project_name}-{region}` | ECS 태스크 실행 역할 (ECR pull, CloudWatch Logs) |
| `role-agentcore-gateway-websearch-for-{project_name}` | AgentCore Web Search Gateway 서비스 역할 |
| `role-s3files-sync-for-{project_name}` | S3 Files ↔ S3 bucket 동기화 역할 (`elasticfilesystem.amazonaws.com` trust) |

> AgentCore Runtime 실행 역할(`AmazonBedrockAgentCoreRuntimeRoleFor{project_name}`)은 `runtime_agent/strands/installer.py`에서 생성하며, S3 Files 사용 시 `s3files:ClientMount` 등 권한이 조건부로 추가됩니다. ECS Task Role에는 `ensure_ecs_task_s3files_policy()`로 mount 권한이 추가됩니다.

> `create_agent_role()`은 배포 경로에서 제거되었습니다. `create_lambda_role()`, `create_agentcore_memory_role()` 함수는 코드에 남아 있으나 `main()`에서는 호출되지 않습니다. uninstaller는 기존 `role-agent-for-…` 정리를 위해 이름 목록을 유지합니다.

#### Knowledge Base 역할 Trust Policy

Bedrock 서비스가 역할을 assume할 수 있도록 AWS 권장 형식을 사용합니다 (`_bedrock_knowledge_base_trust_policy()`).

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "bedrock.amazonaws.com" },
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": { "aws:SourceAccount": "{account_id}" },
      "ArnLike": {
        "aws:SourceArn": "arn:aws:bedrock:{region}:{account_id}:knowledge-base/*"
      }
    }
  }]
}
```

역할 생성 후 IAM 전파를 위해 15초 대기하고, inline policy 부착 여부를 확인합니다 (`wait_for_iam_role_propagation()`).

### 3. S3 Vectors (벡터 스토어)
- **벡터 버킷**: `{project_name}-{account_id}`
- **인덱스**: `{project_name}` (1024차원, cosine, float32)
- **메타데이터**: Bedrock 필수 키(`AMAZON_BEDROCK_TEXT`, `AMAZON_BEDROCK_METADATA`)를 non-filterable로 설정

### 4. Bedrock Knowledge Base
- **스토리지**: S3 Vectors (`S3_VECTORS` 타입)
- **임베딩 모델**: Amazon Titan Embed Text v2 (1024차원, FLOAT32)
- **파싱**: 기본 파서 (default parser)
- **청킹**: Fixed Size (300 토큰, 20% 오버랩)
- **데이터 소스**: S3 `docs/` 프리픽스
- **재시도**: 역할 assume 실패(`ValidationException`) 시 최대 6회 재시도

> `create_opensearch_collection()` 함수는 이전 버전 호환을 위해 코드에 남아 있으나, 현재 배포 흐름에서는 사용하지 않습니다.

### 5. AgentCore Web Search Gateway
- **리전**: `us-west-2` (`AGENTCORE_GATEWAY_REGION`)
- **게이트웨이 이름**: `gateway-websearch`
- **타겟 이름**: `websearch`
- **역할**: `role-agentcore-gateway-websearch-for-{project_name}`
- MCP `websearch` 도구에서 사용하는 AgentCore Gateway URL이 `application/config.json`에 기록됩니다.

### 6. VPC 네트워킹

```
VPC (10.20.0.0/16)
├── Public Subnets (2개 AZ)
│   ├── Internet Gateway 연결
│   └── NAT Gateway 호스팅
├── Private Subnets (2개 AZ)
│   └── NAT Gateway + VPC Endpoints (아웃바운드)
├── Security Groups
│   ├── ALB SG (포트 80)
│   ├── ECS SG (포트 8501, 443)
│   ├── agent-runtime-sg-for-{project_name} (AgentCore microVM)
│   └── s3files-mount-sg-for-{project_name} (NFS 2049)
└── VPC Endpoints
    ├── Interface: bedrock-runtime, ecr.api, ecr.dkr, logs,
    │              secretsmanager, bedrock-agentcore,
    │              bedrock-agentcore-control
    └── Gateway: S3 (ECR 레이어 pull용)
```

### 6.5. S3 Files (Runtime session + ECS app-data)

VPC 생성 직후 session FS(`[5.5]`)와 app-data FS(`[5.6]`)를 **멱등**으로 프로비저닝합니다.

| Prefix | Mount | Client | API |
|--------|-------|--------|-----|
| `agentcore-sessions/` | `/mnt/workspace` | Runtime only | `create_s3_files_session_storage()` |
| `app-data/` | `/mnt/app-data` | ECS only | `create_s3_files_app_data_storage()` |

| 리소스 | 설명 |
|--------|------|
| Sync IAM role | `role-s3files-sync-for-{project_name}` — S3 bucket ↔ NFS 동기화 |
| Session FS | prefix `agentcore-sessions/` — skills, artifacts, checkpoints |
| App-data FS | prefix `app-data/` — graph, settings, litellm |
| Mount targets / Access points | FS별 private subnet + POSIX `uid/gid: 0/0` |

- **AgentCore**: session FS → `/mnt/workspace`
- **ECS**: app-data FS → `/mnt/app-data` (graph / settings)
- 배포 시 `agentcore-sessions/` → `app-data/` 마이그레이션 후 민감 객체(session 쪽 `application-database/`, `litellm/`) 제거
- Runtime IAM: `app-data/*`·`agentcore-sessions/*` S3 API Deny; UI skills는 S3 API로 `agentcore-sessions/{user}/skills/` 조회
- Runtime SG를 Bedrock/AgentCore/Secrets VPC endpoint에 연결

`apply_s3_files_config()`가 `application/config.json`에 session + app-data `s3_files_*`, `agent_runtime_vpc_*` 키를 기록합니다.  
`runtime_agent/strands/installer.py`는 access point ARN이 있으면 **`s3FilesAccessPoint` + VPC 모드**, 없으면 managed **`sessionStorage` + PUBLIC** 으로 Runtime을 생성합니다.

### 7. Application Load Balancer
- **타입**: Internet-facing Application Load Balancer
- **리스너**: HTTP 포트 80
- **타겟 그룹**: ECS Fargate 태스크 (IP 타겟, 포트 8501)
- **헬스체크**: `/api/health`
- **Idle timeout**: 120초 (`ALB_IDLE_TIMEOUT_SECONDS`) — 장시간 SSE 스트림 유지
- **Stickiness**: `app_cookie` on `agent_user_id` (86400초). ECS task 단위 in-memory run 상태(스트리밍·cancel) 일관성용.  
  `lb_cookie`(AWSALB/AWSALBCORS)는 Secure/HttpOnly를 설정할 수 없어 사용하지 않음 ([AWS guidance](https://repost.aws/knowledge-center/elb-secure-flag-alb-cookies)).  
  세션 쿠키는 앱이 HttpOnly + Secure(HTTPS) + SameSite=Lax로 발급.
- **Origin 보호**: listener default = **403 fixed-response**, `X-Custom-Header` 일치 시에만 ECS target group으로 forward (`ensure_alb_listener_origin_protection`)

### 8. CloudFront 배포
- **오리진**:
  - 기본: ALB (동적 컨텐츠) — Secrets Manager 오리진 헤더를 Custom Header로 주입
  - `/images/*`, `/docs/*`, `/artifacts/*`: S3 (정적 컨텐츠)
- **캐시 정책**: Managed-CachingDisabled
- **프로토콜**: HTTP → HTTPS 리다이렉트
- **Origin read timeout**: 120초 (`SSE_ORIGIN_READ_TIMEOUT_SECONDS`)
- **재사용**: 동일 `project_name`의 기존 CloudFront 배포가 있으면 재사용 (헤더·타임아웃·`/artifacts/*` behavior 갱신)

### 8.5. Secrets Manager (ALB origin header)
- **이름**: `{project_name}/cloudfront-alb-origin-header`
- **용도**: CloudFront → ALB 오리진 검증용 `X-Custom-Header` 값 (랜덤, 소스 하드코딩 없음)
- **생성**: `get_or_create_alb_origin_header()` / 삭제: `uninstaller.delete_alb_origin_header_secret()`

### 9. ECR (Elastic Container Registry)
- **리포지토리**: `ecr-for-{project_name}`
- **이미지 태그**: 배포 시각 기반 (`YYYYMMDDHHMMSS`) + ECR에서 `latest`로 promote
- **플랫폼**: `linux/arm64` (AgentCore Runtime과 동일)
- **빌드 소스**: 프로젝트 루트 multi-stage `Dockerfile` (Node frontend + Python FastAPI)
- **빌드 방식**: `docker buildx build --platform linux/arm64 --provenance=false --sbom=false --push`

### 10. ECS Fargate
- **클러스터**: `cluster-for-{project_name}`
- **서비스**: `service-for-{project_name}`
- **태스크 정의**: `task-for-{project_name}`
- **런타임 플랫폼**: `ARM64` / `LINUX` (`runtimePlatform`)
- **컨테이너**: `app` (포트 8501, `uvicorn application.server:app`)
- **CPU / Memory**: 1024 / 2048
- **배포 위치**: Private Subnet (퍼블릭 IP 없음)
- **컨테이너 헬스체크**: `curl -f http://localhost:8501/api/health`
- **볼륨**: S3 Files → `/mnt/app-data` (설정 시)
- **배포 설정**: `minimumHealthyPercent=0`, `maximumPercent=100`, AZ rebalancing DISABLED
- **로그**: CloudWatch Logs `/ecs/app-for-{project_name}`

### 11. AgentCore Runtime (Strands)

VPC·S3 Files 프로비저닝 **후**, CloudFront로 `sharing_url`이 반영된 뒤 루트 installer가 `runtime_agent/strands/installer.py`를 자동 호출합니다 (`[11/10]`).

| 런타임 | 설치 스크립트 | Runtime 이름 |
|--------|--------------|--------------|
| Strands Agent | `runtime_agent/strands/installer.py` | `agent_runtime_name(project)` → `strands_runtime` |

Runtime installer가 생성·갱신하는 주요 리소스:
- IAM: `AmazonBedrockAgentCoreRuntimePolicyFor{project_name}`, `AmazonBedrockAgentCoreRuntimeRoleFor{project_name}`
- AgentCore Runtime: `strands_runtime` (하이픈 → 언더스코어)
- **Session storage (기본)**: S3 Files `s3FilesAccessPoint` @ `/mnt/workspace` + `networkMode: VPC`
- **Fallback**: managed `sessionStorage` + `PUBLIC`
- CloudWatch Logs: `/aws/bedrock-agentcore/runtimes/{runtime_name}-...-DEFAULT`

---

## 주요 함수

### 인프라 생성 함수

#### `create_s3_bucket()`
S3 버킷 생성, CORS·퍼블릭 액세스 차단, **versioning Enabled**

#### `create_knowledge_base_role()` / `create_ecs_roles()` / `create_agentcore_websearch_gateway_role()`
각 서비스별 IAM 역할 및 least-privilege 인라인 정책 생성. KB 역할은 Trust Policy 갱신 후 `wait_for_iam_role_propagation()` 호출.

#### `create_s3_vectors_store()` / `create_knowledge_base_with_s3_vectors()`
S3 Vectors 벡터 버킷·인덱스 생성 및 Bedrock Knowledge Base 연결 (assume 실패 시 재시도)

#### `create_vpc()` / `create_alb()` / `create_cloudfront_distribution()`
VPC·ALB·CloudFront 생성

- VPC: Bedrock Runtime + `ensure_private_subnet_vpc_endpoints()` (ECR, Logs, Secrets, AgentCore, S3 gateway)
- ALB: `ensure_alb_idle_timeout()` (120초), SG는 CloudFront prefix list만 허용
- CloudFront: ALB 오리진에 `X-Custom-Header` 주입, origin read timeout 120초, `/images/*`·`/docs/*`·`/artifacts/*`

#### `get_or_create_alb_origin_header()` / `ensure_alb_listener_origin_protection()`
Secrets Manager에 오리진 헤더 시크릿을 생성·재사용하고, ALB listener를 default 403 + 헤더 일치 시 forward로 맞춤

#### `create_s3_files_session_storage(vpc_info, s3_bucket_name)`
Runtime 전용 S3 Files (`agentcore-sessions/`) 프로비저닝 (멱등).

#### `create_s3_files_app_data_storage(vpc_info, s3_bucket_name, ...)`
ECS 전용 S3 Files (`app-data/`) 프로비저닝 + 마이그레이션 + ECS IAM/FS policy.

#### `apply_s3_files_config(app_config, s3_files_info, s3_files_app_data_info=None)`
session + app-data S3 Files·VPC 키를 `application/config.json`에 병합

#### `create_ecr_repository()` / `build_and_push_docker_image()`
ECR 생성, ARM64 Docker buildx 빌드·push (타임스탬프 태그 → `latest` promote)

#### `deploy_ecs_service(..., s3_files_info=None)`
ECS Fargate 배포 (**app-data** S3 Files 볼륨 `/mnt/app-data`, stickiness, `/api/health`)

#### `get_or_create_agentcore_websearch_gateway()`
AgentCore Web Search gateway 및 managed web-search 타겟 생성/조회

#### `sync_application_capability_lists()`
`runtime_agent/strands/mcp.list`, `skills.list` → `application/` 복사

#### `build_app_environment()` / `write_application_config()` / `_merge_runtime_agent_settings()`
`application/config.json` 생성·병합 (`runtime_agent/strands/config.json`의 Runtime ARN 포함)

#### `install_agent_runtime(runtime_type="strands")`
`runtime_agent/strands/installer.py` subprocess 실행. Runtime 이름은 `agent_runtime_name()`으로 로그에 표시됩니다.

### 헬퍼 함수

| 함수 | 설명 |
|------|------|
| `agent_runtime_name()` | `project_name`의 `-` → `_` |
| `_bedrock_knowledge_base_trust_policy()` / `wait_for_iam_role_propagation()` | KB Trust Policy·IAM 전파 |
| `s3_vectors_bucket_arn()` / `s3_vectors_index_arn()` | S3 Vectors ARN |
| `ensure_private_subnet_vpc_endpoints()` | ECR/Logs/Secrets/AgentCore/S3 엔드포인트 |
| `ensure_alb_idle_timeout()` | ALB idle timeout 120초 |
| `get_or_create_alb_origin_header()` | Secrets Manager 오리진 헤더 생성·재사용 |
| `ensure_alb_listener_origin_protection()` | ALB default 403 + 커스텀 헤더 forward |
| `_ensure_cloudfront_alb_origin_config()` / `_ensure_cloudfront_s3_path_behavior()` | CF 헤더·타임아웃·S3 path |
| `_get_or_create_s3files_*` / `ensure_ecs_task_s3files_policy` | S3 Files 프로비저닝 |
| `_ensure_agent_runtime_vpc_endpoint_access()` | Runtime SG → VPC endpoint |
| `_ensure_native_buildx_builder()` / `_ensure_docker_disk_space()` / `_promote_ecr_image_tag()` | Docker 빌드 |
| `create_alb_target_group_for_ecs()` | IP 타겟 그룹 + stickiness |
| `_wait_for_ecs_service_ready()` | ECS 안정화 대기 |
| `check_application_ready()` | CloudFront readiness |
| `build_config_from_deployment_state()` | 부분 배포 config 복구 |

### 레거시 함수 (main()에서 미사용)

| 함수 | 설명 |
|------|------|
| `create_opensearch_collection()` | OpenSearch Serverless (레거시) |
| `create_lambda_role()` / `create_agentcore_memory_role()` | 미사용 IAM |
| `get_setup_script()` / `run_setup_script_via_ssm()` | EC2 SSM 설정 |
| `create_ec2_instance()` / `create_alb_target_group_and_listener()` | EC2 배포 |
| `verify_ec2_subnet_deployment()` | EC2 서브넷 검증 |

---

## 실행 방법

### 기본 실행 (전체 인프라 배포)

```bash
python installer.py
```

ARM64 EC2에서 Docker buildx로 `linux/arm64` Web UI 이미지를 빌드·push하고, Strands Agent Runtime을 설치한 뒤 ECS Fargate(ARM64) 서비스를 생성합니다.

로컬 테스트 (config 선기록 후):

```bash
uvicorn application.server:app --host 0.0.0.0 --port 8501
```

### Docker 빌드 생략

```bash
python installer.py --skip-docker-build
```

`application/config.json`의 `latest_image_tag`/`build_number`, 또는 ECR 최신 태그, 없으면 `:latest`를 사용합니다.

### Agent Runtime만 별도 설치

```bash
python installer.py --install-agent-runtime
python installer.py --install-agent-runtime strands
```

### 레거시: EC2 SSM 설정 / 서브넷 검증

```bash
python installer.py --run-setup
python installer.py --run-setup i-1234567890abcdef0
python installer.py --verify-deployment
```

---

## 배포 순서

```
[1/10] S3 버킷 생성 (versioning Enabled)
       ↓
[2/10] IAM 역할 생성
       • Knowledge Base / Agent / ECS Task·Execution
       • AgentCore Web Search gateway 역할·gateway (us-west-2)
       ↓
[4/10] S3 Vectors 스토어 생성
       ↓
[4.5/10] Bedrock Knowledge Base 생성
       • Trust Policy 전파 대기·assume 재시도
       ↓
[5/10] VPC 네트워킹
       • VPC, 서브넷, IGW, NAT, ALB/ECS SG
       • VPC 엔드포인트 (Bedrock, ECR, Logs, Secrets, AgentCore, S3)
       ↓
[5.5/10] S3 Files session storage (Runtime / agentcore-sessions/)
       ↓
[5.6/10] S3 Files app-data storage (ECS / app-data/) + migration
       ↓
[5.7/10] ALB origin header (Secrets Manager 랜덤 생성·재사용)
       ↓
[6/10] Application Load Balancer (idle timeout 120초)
       ↓
[7/10] CloudFront (ALB 오리진 헤더 주입 + /images,/docs,/artifacts → S3)
       ↓
[8/10] 앱 설정·Runtime·컨테이너 이미지
       • mcp.list / skills.list 동기화 (runtime_agent/strands/)
       • application/config.json (S3 Files + sharing_url)
       • [11/10] runtime_agent/strands/installer.py
       • ECR + buildx linux/arm64 빌드·push
       ↓
[9/10] ECS Fargate 배포
       • ALB listener: default 403, 헤더 일치 시 forward
       • stickiness + S3 Files /mnt/app-data
       ↓
[10/10] CloudFront readiness 확인
       ↓
완료 - application/config.json 업데이트 (finally)
```

---

## AgentCore Runtime installer (별도)

```bash
cd runtime_agent/strands
python installer.py
```

| 단계 | 설명 |
|------|------|
| KB / config | 루트 `application/config.json`과 KB 설정 반영 |
| IAM | Runtime 정책·역할 (Bedrock AgentCore, ECR, Logs, S3 Files 등) |
| ECR push | Runtime Dockerfile `linux/arm64` 빌드 |
| create_agent_runtime | `s3FilesAccessPoint` + VPC 또는 `sessionStorage` fallback |

Runtime 이름: `agent_runtime_name("strands-runtime")` → `strands_runtime`  
완료 후 `agent_runtime_arn` / `agent_runtime_role`이 `application/config.json`에 병합됩니다.

---

## 배포 완료 후

```
================================================================
Infrastructure Deployment Completed Successfully!
================================================================
Summary:
  S3 Bucket: storage-for-strands-runtime-{account_id}-us-west-2
  VPC ID: vpc-xxxxxxxxx
  Public Subnets: subnet-xxx, subnet-yyy
  Private Subnets: subnet-aaa, subnet-bbb
  ALB DNS: http://alb-for-strands-runtime-xxxxxx.us-west-2.elb.amazonaws.com/
  CloudFront Domain: https://xxxxxxxxx.cloudfront.net
  ECS Service: service-for-strands-runtime (Fargate in private subnet)
  ECR Image: {account_id}.dkr.ecr.us-west-2.amazonaws.com/ecr-for-strands-runtime:YYYYMMDDHHMMSS
  Build Number: YYYYMMDDHHMMSS
  S3 Vector Bucket: strands-runtime-{account_id}
  S3 Vector Index ARN: arn:aws:s3vectors:...
  Knowledge Base ID: XXXXXXXXXX
  Knowledge Base Role: arn:aws:iam::...
  AgentCore Web Search Gateway: gateway-websearch (...)
  AgentCore Web Search Gateway URL: https://...
  S3 Files Access Point: arn:aws:s3files:...
  Agent Runtime Subnets: subnet-aaa, subnet-bbb

Total deployment time: XX.XX minutes
================================================================
```

### application/config.json

배포 성공/실패와 관계없이 `finally` 블록에서 `application/config.json`이 갱신됩니다.

| 필드 | 설명 |
|------|------|
| `projectName`, `accountId`, `region` | 프로젝트 기본 정보 |
| `knowledge_base_id`, `data_source_id` | Bedrock Knowledge Base |
| `knowledge_base_role` | Knowledge Base IAM 역할 ARN |
| `vector_bucket_name`, `vector_bucket_arn` | S3 Vectors 버킷 |
| `vector_index_name`, `vector_index_arn` | S3 Vectors 인덱스 |
| `s3_bucket`, `s3_arn` | 문서·세션 저장 S3 버킷 |
| `s3_files_file_system_id` | Session S3 Files file system ID |
| `s3_files_access_point_arn` | Session S3 Files access point ARN |
| `s3_files_app_data_file_system_id` | App-data S3 Files file system ID |
| `s3_files_app_data_access_point_arn` | App-data S3 Files access point ARN |
| `s3_files_app_data_mount_path` | ECS mount path (`/mnt/app-data`) |
| `agent_runtime_vpc_subnets` | AgentCore Runtime VPC subnet ID 목록 |
| `agent_runtime_security_groups` | AgentCore Runtime security group ID 목록 |
| `sharing_url` | CloudFront URL |
| `agent_runtime_arn`, `agent_runtime_role` | AgentCore Strands Runtime (`runtime_agent/strands/config.json`에서 병합) |
| `agentcore_websearch_gateway_*` | Web Search Gateway ID, URL, 역할 ARN |
| `latest_image_tag`, `build_number` | ECR 이미지 빌드 태그 |
| `collectionArn`, `opensearch_url` | 레거시 호환용 빈 값 |

ECS 컨테이너에는 `APP_CONFIG_JSON` 환경변수로 동일한 설정이 주입되며, `docker-entrypoint.sh`가 시작 시 `application/config.json`으로 기록합니다. `TASK_DB_PROJECT=strands-runtime`도 주입됩니다.

### Docker Container 구성

ECS Web UI는 프로젝트 루트의 multi-stage `Dockerfile`로 빌드됩니다. Agent 추론은 AgentCore Runtime(`runtime_agent/strands/`)에서 별도 `linux/arm64` 이미지로 배포됩니다.

```text
# Stage 1: frontend build
FROM node:22-alpine AS frontend
WORKDIR /web
COPY application/web/package.json application/web/package-lock.json ./
RUN npm ci
COPY application/web/ .
RUN npm run build

# Stage 2: Python runtime
FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install fastapi python-multipart uvicorn[standard] boto3 \
    langchain_aws langchain-openai "openai>=2.41.0" \
    aws-bedrock-token-generator requests
COPY . .
COPY --from=frontend /web/dist /app/application/web/dist
RUN chmod +x /app/docker-entrypoint.sh
EXPOSE 8501
HEALTHCHECK CMD curl --fail http://localhost:8501/api/health
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "application.server:app", "--host", "0.0.0.0", "--port", "8501", "--no-server-header"]
```

### 주의사항
- Docker·ECS·AgentCore 모두 **ARM64** 전용입니다. x86 Mac/EC2에서는 실패하므로 t4g/m7g 등에서 실행하세요.
- CloudFront 배포는 완전히 활성화되기까지 15-20분이 소요될 수 있습니다
- ECS Fargate 서비스가 안정화되고 ALB 헬스체크가 통과하기까지 수 분이 걸릴 수 있습니다
- Knowledge Base 생성 직후 IAM 전파 지연으로 assume 오류가 날 수 있으며, 스크립트가 자동 재시도합니다
- installer 실행 주체(EC2 인스턴스 역할 등)에 Knowledge Base 역할에 대한 `iam:PassRole` 권한이 필요합니다
- Knowledge Base가 기존 OpenSearch Serverless를 사용 중이면 S3 Vectors로 마이그레이션 시 자동 삭제 후 재생성됩니다
- Private Subnet의 Fargate 태스크는 NAT Gateway 및 VPC Endpoint를 통해 ECR에서 이미지를 pull합니다
- S3 Files 사용 시 AgentCore Runtime은 **VPC 모드**이며, mount target AZ·SG(2049)가 맞아야 invoke가 성공합니다
- S3 bucket **versioning Enabled**가 없으면 file system 생성이 실패합니다 (`ValidationException`)
- Managed `sessionStorage`만 사용할 경우 Runtime **Version 업데이트 시** `/mnt/workspace` 세션이 초기화됩니다 (S3 Files 권장)
- 장시간 SSE tool run은 ALB/CloudFront 타임아웃(120초) 설정에 의존합니다

---

## 에러 처리

| 상황 | 처리 방법 |
|------|----------|
| 리소스 이미 존재 | 기존 리소스 재사용 |
| IAM 역할 이미 존재 | Trust Policy 및 inline policy 갱신 |
| KB 역할 assume 실패 | IAM 전파 대기 후 최대 6회 재시도 |
| KB 스토리지 불일치 | Knowledge Base 삭제 후 S3 Vectors로 재생성 |
| ECS 서비스 이미 존재 | 새 태스크 정의로 서비스 업데이트 (`forceNewDeployment`) |
| CIDR 충돌 | 대체 CIDR 블록 자동 선택 |
| 비-ARM64 / Docker 디스크 부족 | 빌드 단계 즉시 실패 |
| S3 Files file system 생성 실패 | bucket versioning 미활성 → `_ensure_s3_bucket_versioning_enabled()` 자동 처리 |
| 배포 실패 | 가능한 배포 정보를 `application/config.json`에 저장 |

### Knowledge Base assume 역할 오류

```
Bedrock Knowledge Base was unable to assume the given role.
```

확인 사항:
1. `role-knowledge-base-for-{project_name}-{region}` Trust Policy에 `bedrock.amazonaws.com` 포함
2. installer 실행 주체에 `iam:PassRole` 권한
3. IAM 전파 완료 후 재실행 (스크립트 자동 재시도 포함)

### S3 Files file system 생성 오류

```
Your bucket must have versioning enabled to create a file system.
```

- `create_s3_bucket()`은 신규 bucket에 versioning **Enabled** 설정
- 기존 bucket은 `create_s3_files_session_storage()` 내 `_ensure_s3_bucket_versioning_enabled()`가 자동 활성화
- sync role(`role-s3files-sync-for-{project_name}`) 및 S3/EventBridge inline policy 확인

---

## 인프라 삭제

```bash
python uninstaller.py
```

삭제 순서(요약): CloudFront 비활성화 → **AgentCore Runtime** (`runtime_agent/strands/uninstaller.py` 위임) → ECS → ALB → EC2(레거시) → NAT → **S3 Files** (access point / mount target / file system / sync role) → VPC → Knowledge Base / S3 Vectors → Gateway / **ALB origin header secret** / IAM / S3 bucket → CloudFront 완전 삭제 → **`application/config.json`**, `runtime_agent/strands/config.json` 정리

단독으로 Runtime만 제거할 때는:

```bash
cd runtime_agent/strands
python uninstaller.py
```
