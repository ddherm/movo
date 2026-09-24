"""SQLite state shared by the web, voice, and coding paths.

Every decision and state transition uses a short IMMEDIATE transaction.  The
database is the source of truth; a voice session never maintains its own copy.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .privacy import has_credential, redact, redact_data


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


def compact_json(value: Any) -> str:
    return json.dumps(redact_data(value), ensure_ascii=False, separators=(",", ":"))


class StateError(ValueError):
    pass


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS project (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    idea TEXT NOT NULL, phase TEXT NOT NULL,
                    spec_version INTEGER NOT NULL DEFAULT 1,
                    agent_conversation_id TEXT,
                    current_plan_id TEXT, pending_question_id TEXT,
                    preview_url TEXT, last_error TEXT, source_dir TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS active_project (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    project_id TEXT NOT NULL REFERENCES project(id)
                );
                CREATE TABLE IF NOT EXISTS spec_versions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    version INTEGER NOT NULL, text TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(project_id, version),
                    FOREIGN KEY(project_id) REFERENCES project(id)
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    name TEXT NOT NULL, mime TEXT NOT NULL,
                    relative_path TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES project(id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    role TEXT NOT NULL, channel TEXT NOT NULL,
                    text TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES project(id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    kind TEXT NOT NULL, summary TEXT NOT NULL,
                    detail_json TEXT, created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES project(id)
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    spec_version INTEGER NOT NULL, status TEXT NOT NULL,
                    body_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    approved_at TEXT,
                    FOREIGN KEY(project_id) REFERENCES project(id)
                );
                CREATE TABLE IF NOT EXISTS questions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    spec_version INTEGER NOT NULL, status TEXT NOT NULL,
                    body_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    answered_at TEXT,
                    FOREIGN KEY(project_id) REFERENCES project(id)
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY, question_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL, spec_version INTEGER NOT NULL,
                    option_id TEXT NOT NULL, reason TEXT NOT NULL,
                    channel TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(question_id) REFERENCES questions(id)
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL, kind TEXT NOT NULL,
                    text TEXT NOT NULL, created_at TEXT NOT NULL,
                    delivery_attempted_at TEXT,
                    delivered_at TEXT,
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(project_id, dedupe_key),
                    FOREIGN KEY(project_id) REFERENCES project(id)
                );
                CREATE TABLE IF NOT EXISTS verification (
                    project_id TEXT PRIMARY KEY,
                    build_status TEXT NOT NULL,
                    interaction_status TEXT NOT NULL,
                    details TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES project(id)
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(notifications)")}
            if "delivery_attempted_at" not in columns:
                conn.execute("ALTER TABLE notifications ADD COLUMN delivery_attempted_at TEXT")
            if "delivered_at" not in columns:
                conn.execute("ALTER TABLE notifications ADD COLUMN delivered_at TEXT")
            if "delivery_attempts" not in columns:
                conn.execute("ALTER TABLE notifications ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0")

            # Existing installations had one project and a globally unique
            # notification key. Keep that project active and preserve its rows.
            conn.execute("BEGIN IMMEDIATE")
            try:
                project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(project)")}
                if "source_dir" not in project_columns:
                    conn.execute("ALTER TABLE project ADD COLUMN source_dir TEXT")
                legacy_notification_key = any(
                    bool(index["unique"]) and [
                        column["name"] for column in conn.execute(f'PRAGMA index_info("{index["name"]}")')
                    ] == ["dedupe_key"]
                    for index in conn.execute("PRAGMA index_list(notifications)")
                )
                if legacy_notification_key:
                    conn.execute(
                        """CREATE TABLE notifications_migrated (
                            id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                            dedupe_key TEXT NOT NULL, kind TEXT NOT NULL,
                            text TEXT NOT NULL, created_at TEXT NOT NULL,
                            delivery_attempted_at TEXT, delivered_at TEXT,
                            delivery_attempts INTEGER NOT NULL DEFAULT 0,
                            UNIQUE(project_id, dedupe_key),
                            FOREIGN KEY(project_id) REFERENCES project(id)
                        )"""
                    )
                    conn.execute(
                        """INSERT INTO notifications_migrated
                           SELECT id, project_id, dedupe_key, kind, text, created_at,
                                  delivery_attempted_at, delivered_at, delivery_attempts
                           FROM notifications"""
                    )
                    conn.execute("DROP TABLE notifications")
                    conn.execute("ALTER TABLE notifications_migrated RENAME TO notifications")
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS project_source_dir_unique ON project(source_dir) WHERE source_dir IS NOT NULL")
                conn.execute(
                    """INSERT OR IGNORE INTO active_project (id,project_id)
                       SELECT 1,id FROM project ORDER BY created_at,rowid LIMIT 1"""
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _project(conn: sqlite3.Connection) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT project.* FROM project JOIN active_project ON active_project.project_id=project.id WHERE active_project.id=1"
        ).fetchone()

    def active_project_id(self) -> str | None:
        with self._connect() as conn:
            project = self._project(conn)
            return str(project["id"]) if project else None

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            projects = [dict(row) for row in conn.execute(
                """SELECT project.id, project.idea, project.phase, project.source_dir,
                          project.preview_url, project.created_at, project.updated_at,
                          (project.id = active_project.project_id) AS active
                   FROM project LEFT JOIN active_project ON active_project.id=1
                   ORDER BY project.updated_at DESC, project.rowid DESC"""
            )]
            for project in projects:
                project["active"] = bool(project["active"])
            return projects

    def select_project(self, project_id: str) -> dict[str, Any]:
        with self._tx() as conn:
            target = conn.execute("SELECT id FROM project WHERE id=?", (project_id,)).fetchone()
            if not target:
                raise StateError("项目不存在。")
            current = self._project(conn)
            if current and current["id"] != project_id and current["phase"] in {"BUILD", "VERIFY", "WAITING_DECISION"}:
                raise StateError("当前项目仍在构建或等待决策，请完成后再切换。")
            conn.execute(
                "INSERT INTO active_project (id,project_id) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id",
                (project_id,),
            )
        return self.snapshot()  # type: ignore[return-value]

    def create_project(self, idea: str, source_dir: str | None = None) -> str:
        idea = idea.strip()
        if not idea:
            raise StateError("请先说说你的想法。")
        if has_credential(idea):
            raise StateError("请不要把 API 凭证写进项目想法；请在 Mac 服务端的 .env 文件配置。")
        if source_dir is not None:
            directory = Path(source_dir).expanduser()
            if not directory.is_absolute() or not directory.is_dir():
                raise StateError("项目文件夹必须是已存在的绝对路径。")
            source_dir = str(directory.resolve())
        with self._tx() as conn:
            current = self._project(conn)
            if current and current["phase"] in {"BUILD", "VERIFY", "WAITING_DECISION"}:
                raise StateError("当前项目仍在构建或等待决策，请完成后再新建。")
            project_id, task_id, ts = new_id(), new_id(), now()
            try:
                conn.execute(
                    "INSERT INTO project (id,task_id,idea,phase,source_dir,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (project_id, task_id, idea, "CLARIFY", source_dir, ts, ts),
                )
            except sqlite3.IntegrityError as exc:
                raise StateError("这个项目文件夹已经被其他项目使用。") from exc
            conn.execute(
                "INSERT INTO active_project (id,project_id) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id",
                (project_id,),
            )
            conn.execute(
                "INSERT INTO spec_versions VALUES (?,?,?,?,?,?)",
                (new_id(), project_id, 1, idea, "初始想法", ts),
            )
            self._event(conn, project_id, "project_created", "已记录初始想法，开始澄清需求。")
            return project_id

    @staticmethod
    def _event(conn: sqlite3.Connection, project_id: str, kind: str, summary: str, detail: Any = None) -> str:
        event_id = new_id()
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?)",
            (event_id, project_id, kind, redact(summary), compact_json(detail) if detail is not None else None, now()),
        )
        conn.execute("UPDATE project SET updated_at=? WHERE id=?", (now(), project_id))
        return event_id

    def event(self, kind: str, summary: str, detail: Any = None) -> str:
        with self._tx() as conn:
            project = self._project(conn)
            if not project:
                raise StateError("项目尚未创建。")
            return self._event(conn, project["id"], kind, summary, detail)

    def add_message(self, role: str, text: str, channel: str = "text") -> str:
        if role not in {"user", "assistant", "system"} or channel not in {"text", "voice", "system"}:
            raise StateError("消息类型无效。")
        text = text.strip()
        if not text:
            raise StateError("消息不能为空。")
        if role == "user" and has_credential(text):
            raise StateError("请不要在消息中提供 API 凭证。")
        text = redact(text)
        with self._tx() as conn:
            project = self._project(conn)
            if not project:
                raise StateError("项目尚未创建。")
            message_id = new_id()
            conn.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                (message_id, project["id"], role, channel, text, now()),
            )
            conn.execute("UPDATE project SET updated_at=? WHERE id=?", (now(), project["id"]))
            return message_id

    def add_asset(self, name: str, mime: str, relative_path: str) -> str:
        name = "参考截图" if has_credential(name) else redact(name)
        with self._tx() as conn:
            project = self._project(conn)
            if not project:
                raise StateError("项目尚未创建。")
            if project["phase"] == "WAITING_DECISION":
                raise StateError("请先回答当前待决策问题，再补充参考截图。")
            asset_id = new_id()
            conn.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?,?)",
                (asset_id, project["id"], name, mime, relative_path, now()),
            )
            self._event(conn, project["id"], "asset_added", f"已添加参考截图：{name}")
            if project["phase"] not in {"IDEA", "CLARIFY", "RESEARCH"}:
                version = project["spec_version"] + 1
                conn.execute(
                    "INSERT INTO spec_versions VALUES (?,?,?,?,?,?)",
                    (new_id(), project["id"], version, f"加入新的参考截图：{name}", "用户补充参考材料", now()),
                )
                next_phase = "PAUSED" if project["phase"] in {"BUILD", "VERIFY"} else "CLARIFY"
                conn.execute(
                    "UPDATE project SET spec_version=?,phase=?,preview_url=NULL,updated_at=? WHERE id=?",
                    (version, next_phase, now(), project["id"]),
                )
                conn.execute("DELETE FROM verification WHERE project_id=?", (project["id"],))
                self._event(conn, project["id"], "spec_changed", f"参考截图已加入第 {version} 版需求，旧预览已撤下。")
            return asset_id

    def add_plan(self, body: dict[str, Any], expected_version: int | None = None) -> str:
        required = {"goal", "audience", "flow", "scope", "exclusions", "route", "reasons", "acceptance", "source_scope"}
        if not required.issubset(body):
            raise StateError("方案字段不完整。")
        with self._tx() as conn:
            project = self._project(conn)
            if not project or project["phase"] not in {"CLARIFY", "RESEARCH", "PLAN_REVIEW", "PAUSED"}:
                raise StateError("当前阶段不能提交方案。")
            if expected_version is not None and project["spec_version"] != expected_version:
                raise StateError("方案生成期间需求已更新，旧结果已丢弃。")
            plan_id = new_id()
            conn.execute(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?)",
                (plan_id, project["id"], project["spec_version"], "pending", compact_json(body), now(), None),
            )
            conn.execute(
                "UPDATE project SET phase='PLAN_REVIEW',current_plan_id=?,updated_at=? WHERE id=?",
                (plan_id, now(), project["id"]),
            )
            self._event(conn, project["id"], "plan_ready", "方案已准备好，等待你确认。")
            self._notification(conn, project["id"], f"plan:{plan_id}", "plan_review", "方案待确认")
            return plan_id

    def commit_planner_turn(
        self, expected_version: int, reply: str, body: dict[str, Any] | None,
        expected_project_id: str | None = None,
    ) -> str | None:
        """Commit a model reply and optional plan only for its input spec version."""
        required = {"goal", "audience", "flow", "scope", "exclusions", "route", "reasons", "acceptance", "source_scope"}
        if body is not None and not required.issubset(body):
            raise StateError("方案字段不完整。")
        with self._tx() as conn:
            project = self._project(conn)
            if (
                not project
                or (expected_project_id is not None and project["id"] != expected_project_id)
                or project["spec_version"] != expected_version
                or project["phase"] not in {"CLARIFY", "RESEARCH", "PAUSED"}
            ):
                raise StateError("需求已更新，旧讨论结果已丢弃。")
            conn.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                (new_id(), project["id"], "assistant", "text", redact(reply.strip()), now()),
            )
            if body is None:
                conn.execute("UPDATE project SET updated_at=? WHERE id=?", (now(), project["id"]))
                return None
            plan_id = new_id()
            conn.execute(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?)",
                (plan_id, project["id"], expected_version, "pending", compact_json(body), now(), None),
            )
            conn.execute(
                "UPDATE project SET phase='PLAN_REVIEW',current_plan_id=?,updated_at=? WHERE id=?",
                (plan_id, now(), project["id"]),
            )
            self._event(conn, project["id"], "plan_ready", "方案已准备好，等待你确认。")
            self._notification(conn, project["id"], f"plan:{plan_id}", "plan_review", "方案待确认")
            return plan_id

    @staticmethod
    def _notification(conn: sqlite3.Connection, project_id: str, key: str, kind: str, text: str) -> str | None:
        notification_id = new_id()
        cursor = conn.execute(
            "INSERT OR IGNORE INTO notifications (id,project_id,dedupe_key,kind,text,created_at) VALUES (?,?,?,?,?,?)",
            (notification_id, project_id, key, kind, text, now()),
        )
        return notification_id if cursor.rowcount else None

    def notify_once(self, key: str, kind: str, text: str) -> str | None:
        with self._tx() as conn:
            project = self._project(conn)
            if not project:
                return None
            return self._notification(conn, project["id"], key, kind, text)

    def claim_notification_delivery(self, key: str) -> bool:
        """Claim one attempt; failed sends can retry after a short backoff."""
        with self._tx() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
            cursor = conn.execute(
                "UPDATE notifications SET delivery_attempted_at=?,delivery_attempts=delivery_attempts+1 WHERE project_id=(SELECT project_id FROM active_project WHERE id=1) AND dedupe_key=? AND delivered_at IS NULL AND delivery_attempts<5 AND (delivery_attempted_at IS NULL OR delivery_attempted_at<?)",
                (now(), key, cutoff),
            )
            return cursor.rowcount == 1

    def mark_notification_delivered(self, key: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE notifications SET delivered_at=? WHERE project_id=(SELECT project_id FROM active_project WHERE id=1) AND dedupe_key=? AND delivered_at IS NULL",
                (now(), key),
            )

    def pending_notification_deliveries(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT dedupe_key,kind FROM notifications WHERE project_id=(SELECT project_id FROM active_project WHERE id=1) AND delivered_at IS NULL AND delivery_attempts<5 ORDER BY rowid"
            )]

    def approve_plan(self) -> str:
        with self._tx() as conn:
            project = self._project(conn)
            if not project or project["phase"] != "PLAN_REVIEW" or not project["current_plan_id"]:
                raise StateError("没有待确认的方案。")
            plan = conn.execute("SELECT * FROM plans WHERE id=?", (project["current_plan_id"],)).fetchone()
            if not plan or plan["status"] != "pending" or plan["spec_version"] != project["spec_version"]:
                raise StateError("方案已过期，请重新确认当前版本。")
            conn.execute("UPDATE plans SET status='approved',approved_at=? WHERE id=?", (now(), plan["id"]))
            conn.execute("UPDATE project SET phase='BUILD',updated_at=? WHERE id=?", (now(), project["id"]))
            self._event(conn, project["id"], "plan_approved", "方案已确认，开始隔离工作区构建。")
            return plan["id"]

    def set_phase(self, phase: str, summary: str, kind: str = "phase_changed") -> None:
        if phase not in {"IDEA", "CLARIFY", "RESEARCH", "PLAN_REVIEW", "BUILD", "WAITING_DECISION", "VERIFY", "READY", "FAILED", "PAUSED"}:
            raise StateError("未知阶段。")
        with self._tx() as conn:
            project = self._project(conn)
            if not project:
                raise StateError("项目尚未创建。")
            if project["phase"] == phase:
                return
            conn.execute("UPDATE project SET phase=?,updated_at=? WHERE id=?", (phase, now(), project["id"]))
            self._event(conn, project["id"], kind, summary)
            if phase in {"READY", "FAILED"}:
                self._notification(conn, project["id"], f"phase:{phase}:{project['spec_version']}", phase.lower(), "原型已完成" if phase == "READY" else "任务需要查看")

    def open_question(self, body: dict[str, Any], expected_version: int | None = None) -> str:
        options = body.get("options") or []
        if not body.get("question") or len(options) < 2 or not body.get("recommended_option_id"):
            raise StateError("决策问题必须包含问题、至少两个选项和推荐项。")
        option_ids = {item.get("id") for item in options}
        if body["recommended_option_id"] not in option_ids:
            raise StateError("推荐选项无效。")
        with self._tx() as conn:
            project = self._project(conn)
            if not project or project["phase"] not in {"BUILD", "VERIFY"}:
                raise StateError("当前阶段不能提出构建决策。")
            if expected_version is not None and project["spec_version"] != expected_version:
                raise StateError("需求已更新，旧版本的决策问题已丢弃。")
            if project["pending_question_id"]:
                raise StateError("已有一个待回答问题。")
            question_id = new_id()
            conn.execute(
                "INSERT INTO questions VALUES (?,?,?,?,?,?,?)",
                (question_id, project["id"], project["spec_version"], "open", compact_json(body), now(), None),
            )
            conn.execute(
                "UPDATE project SET pending_question_id=?,phase='WAITING_DECISION',updated_at=? WHERE id=?",
                (question_id, now(), project["id"]),
            )
            self._event(conn, project["id"], "decision_needed", "有一个产品问题需要你决定。", {"question_id": question_id})
            self._notification(conn, project["id"], f"question:{question_id}", "decision", "有一个问题待决定")
            return question_id

    def answer_question(self, question_id: str, option_id: str, reason: str, channel: str) -> dict[str, Any]:
        if channel not in {"text", "voice"}:
            raise StateError("回答渠道无效。")
        if has_credential(reason):
            raise StateError("请不要在决定原因中提供 API 凭证。")
        with self._tx() as conn:
            question = conn.execute(
                "SELECT * FROM questions WHERE id=? AND project_id=(SELECT project_id FROM active_project WHERE id=1)",
                (question_id,),
            ).fetchone()
            if not question:
                raise StateError("问题不存在。")
            existing = conn.execute("SELECT * FROM decisions WHERE question_id=?", (question_id,)).fetchone()
            if existing:
                return {"accepted": False, "decision_id": existing["id"], "message": "这个问题已经处理。"}
            body = json.loads(question["body_json"])
            if option_id not in {option.get("id") for option in body["options"]}:
                raise StateError("请选择卡片中的有效选项。")
            project = self._project(conn)
            if not project or project["pending_question_id"] != question_id or question["status"] != "open":
                raise StateError("问题已不再等待回答。")
            decision_id = new_id()
            conn.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?)",
                (decision_id, question_id, project["id"], project["spec_version"], option_id, reason.strip(), channel, now()),
            )
            conn.execute("UPDATE questions SET status='answered',answered_at=? WHERE id=?", (now(), question_id))
            conn.execute(
                "UPDATE project SET pending_question_id=NULL,phase='BUILD',updated_at=? WHERE id=?",
                (now(), project["id"]),
            )
            self._event(conn, project["id"], "decision_recorded", "你的选择已记录，原任务继续执行。", {"decision_id": decision_id, "option_id": option_id})
            return {"accepted": True, "decision_id": decision_id, "message": "已记录你的决定，继续原任务。"}

    def change_spec(self, text: str, reason: str) -> int:
        text, reason = text.strip(), reason.strip()
        if not text:
            raise StateError("请描述要修改的需求。")
        if has_credential(text) or has_credential(reason):
            raise StateError("请不要在需求变更中提供 API 凭证。")
        with self._tx() as conn:
            project = self._project(conn)
            if not project:
                raise StateError("项目尚未创建。")
            if project["phase"] == "WAITING_DECISION":
                raise StateError("请先回答当前待决策问题。")
            version = project["spec_version"] + 1
            conn.execute(
                "INSERT INTO spec_versions VALUES (?,?,?,?,?,?)",
                (new_id(), project["id"], version, text, reason or "用户在任务中提出修改", now()),
            )
            next_phase = "PAUSED" if project["phase"] in {"BUILD", "VERIFY"} else "CLARIFY"
            conn.execute(
                "UPDATE project SET spec_version=?,phase=?,preview_url=NULL,updated_at=? WHERE id=?",
                (version, next_phase, now(), project["id"]),
            )
            conn.execute("DELETE FROM verification WHERE project_id=?", (project["id"],))
            self._event(conn, project["id"], "spec_changed", f"需求已更新到第 {version} 版，等待安全边界调整。", {"text": text, "reason": reason})
            return version

    def set_conversation_id(self, conversation_id: str) -> None:
        with self._tx() as conn:
            project = self._project(conn)
            if not project:
                raise StateError("项目尚未创建。")
            conn.execute("UPDATE project SET agent_conversation_id=?,updated_at=? WHERE id=?", (conversation_id, now(), project["id"]))

    def set_verification(self, build_status: str, interaction_status: str, details: str, preview_url: str | None = None) -> None:
        with self._tx() as conn:
            project = self._project(conn)
            if not project:
                raise StateError("项目尚未创建。")
            conn.execute(
                "INSERT INTO verification VALUES (?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET build_status=excluded.build_status,interaction_status=excluded.interaction_status,details=excluded.details,updated_at=excluded.updated_at",
                (project["id"], build_status, interaction_status, redact(details), now()),
            )
            if preview_url:
                conn.execute("UPDATE project SET preview_url=?,updated_at=? WHERE id=?", (preview_url, now(), project["id"]))
            self._event(conn, project["id"], "verification", f"构建：{build_status}；核心交互：{interaction_status}。")

    def set_error(self, message: str) -> None:
        with self._tx() as conn:
            project = self._project(conn)
            if not project:
                return
            conn.execute("UPDATE project SET last_error=?,updated_at=? WHERE id=?", (redact(message), now(), project["id"]))
            self._event(conn, project["id"], "error", redact(message))

    def finish_build(self, expected_version: int, details: str, *, from_iteration_limit: bool = False) -> bool:
        """Publish a verified result only for the still-current approved spec."""
        with self._tx() as conn:
            project = self._project(conn)
            if not project or project["spec_version"] != expected_version:
                return False
            if project["phase"] != "BUILD":
                if not (from_iteration_limit and project["phase"] == "FAILED" and "MaxIterationsReached" in (project["last_error"] or "")):
                    return False
                plan = conn.execute("SELECT status,spec_version FROM plans WHERE id=?", (project["current_plan_id"],)).fetchone()
                if not plan or plan["status"] != "approved" or plan["spec_version"] != expected_version:
                    return False
            conn.execute("UPDATE project SET phase='VERIFY',updated_at=? WHERE id=?", (now(), project["id"]))
            self._event(conn, project["id"], "phase_changed", "构建完成，正在核对静态预览与核心交互。")
            conn.execute(
                "INSERT INTO verification VALUES (?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET build_status=excluded.build_status,interaction_status=excluded.interaction_status,details=excluded.details,updated_at=excluded.updated_at",
                (project["id"], "passed", "passed", redact(details), now()),
            )
            conn.execute("UPDATE project SET phase='READY',preview_url='/preview/',last_error=NULL,updated_at=? WHERE id=?", (now(), project["id"]))
            self._event(conn, project["id"], "phase_changed", "原型与验收说明已准备好。")
            self._notification(conn, project["id"], f"phase:READY:{expected_version}", "ready", "原型已完成")
            return True

    def fail_build(self, expected_version: int, reason: str, summary: str, verification: dict[str, str] | None = None) -> bool:
        with self._tx() as conn:
            project = self._project(conn)
            if not project or project["spec_version"] != expected_version or project["phase"] not in {"BUILD", "VERIFY"}:
                return False
            if verification:
                conn.execute(
                    "INSERT INTO verification VALUES (?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET build_status=excluded.build_status,interaction_status=excluded.interaction_status,details=excluded.details,updated_at=excluded.updated_at",
                    (project["id"], verification.get("build_status", "missing"), verification.get("interaction_status", "missing"), redact(verification.get("details", "")), now()),
                )
            conn.execute("UPDATE project SET phase='FAILED',last_error=?,updated_at=? WHERE id=?", (redact(reason[:300]), now(), project["id"]))
            self._event(conn, project["id"], "build_failed", summary)
            self._notification(conn, project["id"], f"phase:FAILED:{expected_version}", "failed", "任务需要查看")
            return True

    def snapshot(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            project = self._project(conn)
            if not project:
                return None
            pid = project["id"]
            result = dict(project)
            result["screenshots"] = [
                {"id": row["id"], "url": f"/api/assets/{row['id']}", "name": row["name"], "mime": row["mime"]}
                for row in conn.execute("SELECT * FROM assets WHERE project_id=? ORDER BY created_at", (pid,))
            ]
            result["messages"] = [dict(row) for row in conn.execute("SELECT id,role,channel,text,created_at FROM messages WHERE project_id=? ORDER BY created_at,rowid", (pid,))]
            result["events"] = [dict(row) for row in conn.execute("SELECT id,kind,summary,created_at FROM events WHERE project_id=? ORDER BY rowid DESC LIMIT 100", (pid,))]
            result["notifications"] = [dict(row) for row in conn.execute("SELECT id,kind,text,created_at FROM notifications WHERE project_id=? ORDER BY rowid DESC LIMIT 30", (pid,))]
            plan = conn.execute("SELECT * FROM plans WHERE id=?", (project["current_plan_id"],)).fetchone() if project["current_plan_id"] else None
            result["plan"] = {**json.loads(plan["body_json"]), "id": plan["id"], "status": plan["status"], "spec_version": plan["spec_version"]} if plan and plan["spec_version"] == project["spec_version"] else None
            result["plan_history"] = [
                {**json.loads(row["body_json"]), "id": row["id"], "status": row["status"], "spec_version": row["spec_version"], "created_at": row["created_at"]}
                for row in conn.execute("SELECT * FROM plans WHERE project_id=? ORDER BY rowid", (pid,))
            ]
            question = conn.execute("SELECT * FROM questions WHERE id=?", (project["pending_question_id"],)).fetchone() if project["pending_question_id"] else None
            result["pending_question"] = {**json.loads(question["body_json"]), "id": question["id"], "spec_version": question["spec_version"]} if question else None
            verification = conn.execute("SELECT build_status,interaction_status,details FROM verification WHERE project_id=?", (pid,)).fetchone()
            result["verification"] = dict(verification) if verification else None
            result["spec_versions"] = [dict(row) for row in conn.execute("SELECT version,text,reason,created_at FROM spec_versions WHERE project_id=? ORDER BY version", (pid,))]
            result["decisions"] = [dict(row) for row in conn.execute("SELECT id,question_id,spec_version,option_id,reason,channel,created_at FROM decisions WHERE project_id=? ORDER BY created_at", (pid,))]
            return result

    def asset_path(self, asset_id: str) -> Path | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT relative_path FROM assets WHERE id=? AND project_id=(SELECT project_id FROM active_project WHERE id=1)",
                (asset_id,),
            ).fetchone()
            return Path(row["relative_path"]) if row else None
