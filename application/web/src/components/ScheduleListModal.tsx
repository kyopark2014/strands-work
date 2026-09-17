import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { api, type ScheduleJob } from "../api";
import type { Task } from "../types";
import { ConfirmDialog, type ConfirmOptions } from "./ConfirmDialog";

type Props = {
  open: boolean;
  onClose: () => void;
  tasks?: Task[];
  onSelectTask?: (taskId: string) => void;
};

const SKIP_CONFIRM_PREFIX = "strands-work:skip-confirm:";

function shouldSkipConfirm(key: string): boolean {
  try {
    return localStorage.getItem(`${SKIP_CONFIRM_PREFIX}${key}`) === "1";
  } catch {
    return false;
  }
}

function setSkipConfirm(key: string): void {
  try {
    localStorage.setItem(`${SKIP_CONFIRM_PREFIX}${key}`, "1");
  } catch {
    /* ignore */
  }
}

function formatWhen(value: string | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function jobTitle(job: ScheduleJob): string {
  return (job.title || job.prompt || job.job_id).trim() || job.job_id;
}

export function ScheduleListModal({ open, onClose, tasks = [], onSelectTask }: Props) {
  const [jobs, setJobs] = useState<ScheduleJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [confirmState, setConfirmState] = useState<{
    options: ConfirmOptions;
    resolve: (ok: boolean) => void;
  } | null>(null);

  const taskById = useMemo(() => {
    const map = new Map<string, Task>();
    for (const task of tasks) {
      map.set(task.id, task);
    }
    return map;
  }, [tasks]);

  const askConfirm = useCallback((options: ConfirmOptions) => {
    if (options.dontAskAgainKey && shouldSkipConfirm(options.dontAskAgainKey)) {
      return Promise.resolve(true);
    }
    return new Promise<boolean>((resolve) => {
      setConfirmState({ options, resolve });
    });
  }, []);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await api.listSchedules();
        if (!cancelled) setJobs(data.jobs || []);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !confirmState) onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, confirmState]);

  async function deleteJob(job: ScheduleJob) {
    const label = jobTitle(job);
    const ok = await askConfirm({
      title: "Delete schedule",
      message: `“${label}” 예약을 삭제할까요?`,
      detail: "EventBridge 스케줄과 저장된 job이 함께 삭제됩니다. 되돌릴 수 없습니다.",
      confirmLabel: "삭제",
      cancelLabel: "취소",
      danger: true,
      dontAskAgainKey: "delete-schedule",
    });
    if (!ok) return;
    setDeletingId(job.job_id);
    setError(null);
    try {
      await api.deleteSchedule(job.job_id);
      setJobs((prev) => prev.filter((j) => j.job_id !== job.job_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeletingId(null);
    }
  }

  if (!open) return null;

  return createPortal(
    <>
      <div
        className="modal-overlay"
        role="dialog"
        aria-modal="true"
        aria-labelledby="schedule-list-title"
        onMouseDown={(e) => {
          if (confirmState) return;
          if (e.target === e.currentTarget) onClose();
        }}
      >
        <div
          className="modal share-list-modal"
          onMouseDown={(e) => e.stopPropagation()}
        >
          <div className="modal-header">
            <h2 id="schedule-list-title" className="modal-title">
              Schedule List
            </h2>
            <button type="button" className="modal-close" aria-label="Close" onClick={onClose}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M6 6l12 12M18 6L6 18" />
              </svg>
            </button>
          </div>

          <div className="share-list-body">
            {loading ? (
              <p className="share-list-muted">예약 목록을 불러오는 중…</p>
            ) : jobs.length === 0 ? (
              error ? (
                <p className="share-list-error" role="alert">
                  {error}
                </p>
              ) : (
                <p className="share-list-muted">
                  등록된 예약이 없습니다. 채팅에서 “매일 아침 8시에 …”처럼 요청하면 my-schedule
                  스킬이 등록합니다.
                </p>
              )
            ) : (
              <>
                {error ? (
                  <p className="share-list-error" role="alert">
                    {error}
                  </p>
                ) : null}
                <ul className="share-doc-list">
                  {jobs.map((job) => {
                    const isDeleting = deletingId === job.job_id;
                    const task = taskById.get(job.task_id);
                    const room = task?.title || job.task_id;
                    const roomExists = Boolean(task);
                    const tz = job.timezone || "Asia/Seoul";
                    const status = job.enabled === false ? "중지됨" : "활성";
                    const sub = `${job.schedule_expression} · ${tz} · ${status} · ${room}`;
                    return (
                      <li key={job.job_id} className="share-doc-list-item">
                        <div className="share-doc-list-meta">
                          <span className="share-doc-list-name" title={jobTitle(job)}>
                            {jobTitle(job)}
                          </span>
                          <span className="share-doc-list-sub" title={sub}>
                            {sub}
                          </span>
                          {job.last_run_at ? (
                            <span className="share-doc-list-sub">
                              최근 실행: {formatWhen(job.last_run_at)}
                              {job.last_run_status ? ` (${job.last_run_status})` : ""}
                            </span>
                          ) : null}
                        </div>
                        <div className="share-doc-list-actions">
                          <button
                            type="button"
                            className="share-doc-list-btn share-doc-list-btn-success"
                            disabled={isDeleting || !onSelectTask || !roomExists}
                            title={
                              roomExists
                                ? "등록한 대화방으로 이동"
                                : "대화방을 찾을 수 없습니다"
                            }
                            onClick={() => {
                              if (!onSelectTask || !roomExists) return;
                              onSelectTask(job.task_id);
                              onClose();
                            }}
                          >
                            열기
                          </button>
                          <button
                            type="button"
                            className="share-doc-list-btn share-doc-list-btn-danger"
                            disabled={isDeleting}
                            title="예약 삭제"
                            onClick={() => void deleteJob(job)}
                          >
                            {isDeleting ? "삭제 중…" : "삭제"}
                          </button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
          </div>

          <div className="modal-footer">
            <span />
            <div className="modal-actions">
              <button type="button" className="send-btn" onClick={onClose}>
                닫기
              </button>
            </div>
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={!!confirmState}
        options={confirmState?.options ?? null}
        onConfirm={(dontAskAgain) => {
          const key = confirmState?.options.dontAskAgainKey;
          if (dontAskAgain && key) setSkipConfirm(key);
          confirmState?.resolve(true);
          setConfirmState(null);
        }}
        onCancel={() => {
          confirmState?.resolve(false);
          setConfirmState(null);
        }}
      />
    </>,
    document.body,
  );
}
