---
name: my-schedule
description: EventBridge Scheduler로 대화방에 예약 작업을 등록합니다. 사용자가 "매일", "매주", "아침마다", "정기적으로", "스케줄", "예약 실행", "알람처럼 알려줘" 등 반복 실행을 요청할 때 사용합니다.
---

# my-schedule

사용자의 요청을 **job**으로 DynamoDB에 저장하고, **EventBridge Scheduler**가 일정에 맞춰 Lambda → ECS Web을 호출합니다.  
실행 결과는 **현재 대화방**에 일반 채팅 메시지처럼 저장됩니다.

## When to Use

- 매일/매주/매시간 등 **반복 작업** 요청
- "아침 8시마다 날씨 알려줘", "매주 월요일 요약해줘"
- 기존 예약 조회·수정·중지·삭제

## Script Location

application working directory 기준 전체 경로를 사용하세요.

| 스크립트 | 용도 |
| --- | --- |
| `skills/my-schedule/scripts/manage_schedule.py` | create / list / get / update / delete / enable / disable |
| `skills/my-schedule/scripts/lib_schedule.py` | HTTP·인증 헬퍼 (직접 실행하지 않음) |

**IMPORTANT**: `scripts/...`로 줄이지 말고 위 전체 경로를 사용하세요.

## Critical Rules

1. **반드시 스크립트로** 예약하세요. AWS CLI/콘솔을 직접 쓰지 마세요.
2. 예약은 **현재 대화방(`TASK_ID`)**에 묶입니다. `--task-id`를 생략하면 환경변수 `TASK_ID`를 씁니다.
3. 실행 시 에이전트가 받을 **prompt**를 명확히 적으세요. (예: `오늘 서울 날씨와 미세먼지를 요약해줘`)
4. 스케줄 표현은 EventBridge Scheduler 형식입니다.
   - 매일 08:00 KST: `cron(0 8 * * ? *)` + `--timezone Asia/Seoul`
   - 1시간마다: `rate(1 hours)`
5. 출력은 JSON입니다. 사용자에게는 한국어로 `job_id`, 시각, prompt를 요약하세요.
6. 인증은 스크립트가 `SCHEDULE_AGENT_TOKEN` / Secrets Manager로 처리합니다.

## Cron 가이드 (KST)

| 요청 | schedule_expression | timezone |
| --- | --- | --- |
| 매일 아침 8시 | `cron(0 8 * * ? *)` | `Asia/Seoul` |
| 매일 밤 10시 | `cron(0 22 * * ? *)` | `Asia/Seoul` |
| 평일 9시 | `cron(0 9 ? * MON-FRI *)` | `Asia/Seoul` |
| 매주 월요일 9시 | `cron(0 9 ? * MON *)` | `Asia/Seoul` |
| 1시간마다 | `rate(1 hours)` | `Asia/Seoul` |
| 30분마다 | `rate(30 minutes)` | `Asia/Seoul` |

형식: `cron(분 시 일 월 요일 년도)` — 일과 요일 중 하나는 `?` 이어야 합니다.

## Quick Start

### 생성 (매일 08:00 날씨)

```bash
python skills/my-schedule/scripts/manage_schedule.py create \
  --prompt "오늘 날씨와 미세먼지를 알려줘" \
  --cron "cron(0 8 * * ? *)" \
  --timezone Asia/Seoul \
  --title "아침 날씨"
```

### 목록

```bash
python skills/my-schedule/scripts/manage_schedule.py list --this-task
python skills/my-schedule/scripts/manage_schedule.py list
```

### 조회 / 중지 / 삭제

```bash
python skills/my-schedule/scripts/manage_schedule.py get <job_id>
python skills/my-schedule/scripts/manage_schedule.py disable <job_id>
python skills/my-schedule/scripts/manage_schedule.py enable <job_id>
python skills/my-schedule/scripts/manage_schedule.py delete <job_id>
```

### 수정

```bash
python skills/my-schedule/scripts/manage_schedule.py update <job_id> \
  --cron "cron(0 9 * * ? *)" \
  --prompt "아침 브리핑: 날씨 + 할 일"
```

## Usage (agent)

```python
import subprocess, json, os
SCRIPT = "skills/my-schedule/scripts/manage_schedule.py"

# create
out = subprocess.check_output([
    "python", SCRIPT, "create",
    "--prompt", "오늘 날씨 알려줘",
    "--cron", "cron(0 8 * * ? *)",
    "--timezone", "Asia/Seoul",
    "--title", "아침 날씨",
], text=True)
print(out)

# list this room
out = subprocess.check_output(
    ["python", SCRIPT, "list", "--this-task"], text=True
)
```

## Architecture (참고)

```
사용자 요청 → manage_schedule.py → ECS /api/schedules
  → DynamoDB job JSON + EventBridge Scheduler 등록
Scheduler 시각 도래 → Lambda(job_id) → DynamoDB 조회
  → ECS /api/internal/schedules/{job_id}/run
  → prompt로 agent 실행 → 대화방에 user/assistant 메시지 저장
```

Job JSON 주요 필드: `job_id`, `user_id`, `task_id`, `runtime_session_id`, `prompt`, `schedule_expression`, `timezone`, `enabled`.

## Env

| 변수 | 설명 |
| --- | --- |
| `TASK_ID` | 현재 대화방 (런타임이 payload로 주입) |
| `RUNTIME_SESSION_ID` | 체크포인트 세션 |
| `USER_ID` / `CURRENT_USER_ID` | 사용자 |
| `SCHEDULE_AGENT_TOKEN` | AgentCore용 HMAC 시크릿 |
| `APP_BASE_URL` / config `sharing_url` | ECS/CloudFront URL |

## Troubleshooting

- `task_id required`: 현재 런타임에 `TASK_ID`가 없음. 앱/런타임을 최신으로 재배포했는지 확인.
- `Schedule infra not configured`: installer로 DynamoDB/Lambda/Scheduler 역할이 아직 없음.
- `Schedule auth unavailable`: Secrets Manager `agentic-work/schedule-agent-token` + AgentCore IAM Allow 확인.
