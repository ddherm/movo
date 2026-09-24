"""DeepSeek powered clarification and proposal drafting.

The JSON contract keeps the model away from state transitions.  The service
validates and commits a proposal only after this module returns it.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx


Search = Callable[[str], Awaitable[list[dict[str, str]]]]


class PlannerUnavailable(RuntimeError):
    pass


class Planner:
    def __init__(self, data_dir: Path, search: Search | None = None, asset_lookup: Callable[[str], Path | None] | None = None):
        self.data_dir = Path(data_dir)
        self.search = search
        self.asset_lookup = asset_lookup

    async def next_turn(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise PlannerUnavailable("DeepSeek API Key 尚未配置；已保存你的需求，配置后可继续讨论。")

        sources: list[dict[str, str]] = []
        source_scope = "未配置联网检索；方案仅基于用户说明、参考截图和模型已有知识。"
        if os.getenv("TAVILY_API_KEY") and self.search:
            try:
                sources = (await self.search(snapshot["idea"]))[:5]
                source_scope = "已联网检索公开资料；请核对下列来源。" if sources else "已尝试联网检索，但没有找到可用来源。"
            except Exception:
                source_scope = "联网检索失败；方案仅基于用户说明、参考截图和模型已有知识。"

        dialogue = [
            {"role": item["role"], "text": item["text"]}
            for item in snapshot["messages"][-20:]
            if item["role"] in {"user", "assistant"}
        ]
        clarification_count = sum(1 for item in dialogue if item["role"] == "assistant" and "？" in item["text"])
        spec_history = snapshot.get("spec_versions", [])
        text_context = json.dumps(
            {
                "initial_idea": snapshot["idea"],
                "spec_version": snapshot["spec_version"],
                "spec_history": spec_history,
                "dialogue": dialogue,
                "clarification_questions_so_far": clarification_count,
                "reference_images": [item["name"] for item in snapshot["screenshots"]],
                "research_scope": source_scope,
                "research_sources": sources,
            }, ensure_ascii=False,
        )
        user_content: list[dict[str, Any]] = [{"type": "text", "text": text_context}]
        if snapshot["screenshots"]:
            latest = snapshot["screenshots"][-1]
            asset_name = latest["url"].rsplit("/", 1)[-1]
            relative = self._asset_relative_path(asset_name)
            if relative:
                image_path = self.data_dir / relative
                if image_path.is_file() and image_path.stat().st_size <= 5 * 1024 * 1024:
                    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
                    user_content.append({"type": "image_url", "image_url": {"url": f"data:{latest['mime']};base64,{encoded}"}})

        system = """你是单人创作者的产品与编程搭档。用简洁中文，先理解目标用户、最重要的操作、成功标准。
如果功能和首版时间/范围冲突，指出具体取舍并给建议，允许用户反驳。最多追问两个高价值产品问题；已有足够信息、用户要求出方案、或问题数已达两次时直接出方案。不要问框架或数据库选择。
只输出 JSON：{"reply":"给用户的简短回复", "ready_to_plan":true/false, "plan":null 或方案对象}。
若 ready_to_plan 为 true，plan 必须含 goal,audience,flow,scope,exclusions,route,reasons,acceptance,source_scope,sources。
除 sources 外的字段用简短中文字符串。acceptance 要写手机上可执行的验证步骤。scope 仅包含静态或轻量交互 Web 原型。route 说明技术路线，reasons 说明推荐原因。
source_scope 和 sources 必须忠实使用输入中的 research_scope/research_sources；没有联网时不得编造网址或声称查过资料。
如有参考图，goal 或 flow 要明确说明参考了哪张图的什么体验。输出不得要求用户口述 API Key。"""
        payload = {
            "model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0.3,
            "max_tokens": 1800,
        }
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        try:
            result = None
            async with httpx.AsyncClient(timeout=90) as client:
                for _attempt in range(2):
                    response = await client.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json=payload,
                    )
                    response.raise_for_status()
                    content = response.json()["choices"][0]["message"]["content"]
                    if not isinstance(content, str) or not content.strip():
                        continue
                    cleaned = content.strip()
                    if cleaned.startswith("```") and cleaned.endswith("```"):
                        cleaned = "\n".join(cleaned.splitlines()[1:-1]).strip()
                    try:
                        result = json.loads(cleaned)
                        break
                    except ValueError:
                        continue
            if not isinstance(result, dict):
                raise ValueError("empty or invalid JSON")
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            raise PlannerUnavailable(f"需求讨论暂时无法连接模型（{type(exc).__name__}）；消息已保存，可稍后重试。") from exc

        reply = str(result.get("reply") or "").strip()
        if not reply:
            raise PlannerUnavailable("模型没有返回可用回复；消息已保存，可稍后重试。")
        if result.get("ready_to_plan"):
            plan = result.get("plan")
            if not isinstance(plan, dict):
                raise PlannerUnavailable("模型未生成完整方案；可以继续讨论后重试。")
            plan["sources"] = sources
            plan["source_scope"] = source_scope
            return {"reply": reply, "plan": plan}
        return {"reply": reply, "plan": None}

    def _asset_relative_path(self, asset_id: str) -> Path | None:
        # Resolve through SQLite instead of trusting a URL or a user filename.
        return self.asset_lookup(asset_id) if self.asset_lookup else None
