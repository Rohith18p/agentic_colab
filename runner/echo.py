"""EchoAgent: a no-LLM stub that exercises the whole loop for free.

It claims open tasks, posts a few progress messages, and completes them. It
echoes back @mentions from humans. If a task's title or description contains
"[ask]", it asks a human first and waits for the answer.
"""

from __future__ import annotations

from typing import Any

from .agent import Agent, AgentContext
from .client import ToolCallError


class EchoAgent(Agent):
    skills = ["echo", "testing"]

    def __init__(self, work_steps: int = 3) -> None:
        self.work_steps = work_steps
        self.task: dict[str, Any] | None = None
        self.progress = 0
        self.waiting_on: int | None = None  # id of the question we asked
        self.answer: dict[str, Any] | None = None
        self.completed: list[int] = []

    async def setup(self, ctx: AgentContext) -> None:
        # Skip anything already in the inbox so a restart does not re-echo old messages.
        while await ctx.read_inbox():
            pass
        await ctx.post_message(f"{ctx.agent_name} online (EchoAgent). I claim open tasks and echo @mentions.")

    async def step(self, ctx: AgentContext) -> bool:
        did_work = await self._handle_inbox(ctx)
        if self.waiting_on is not None:
            return did_work
        try:
            if self.task is None:
                return await self._claim_next(ctx) or did_work
            await self._advance(ctx)
            return True
        except ToolCallError as e:
            if e.code in ("not_your_task", "task_not_found"):
                # Lost the task (lease expired or a human released it); move on.
                self.task = None
                return True
            raise

    async def _handle_inbox(self, ctx: AgentContext) -> bool:
        did_work = False
        for m in await ctx.read_inbox():
            if self.waiting_on is not None and m["reply_to"] == self.waiting_on and m["author_type"] == "human":
                self.answer, self.waiting_on = m, None
                continue
            mentioned = ctx.agent_name.lower() in (n.lower() for n in m["mentions"])
            if mentioned and m["author_type"] == "human" and m["type"] != "control":
                target = {"task_id": m["task_id"]} if m["task_id"] else {"channel": m["channel"]}
                await ctx.post_message(f"@{m['author_name']} echo: {m['body']}", reply_to=m["id"], **target)
                did_work = True
        return did_work

    async def _claim_next(self, ctx: AgentContext) -> bool:
        for t in await ctx.list_tasks(status="open"):
            try:
                self.task = await ctx.claim_task(t["id"], approach_note="Echo: acknowledge, tick through steps, complete.")
            except ToolCallError as e:
                if e.code == "not_claimable":
                    continue  # someone else won the race
                raise
            self.progress = 0
            return True
        return False

    async def _advance(self, ctx: AgentContext) -> None:
        task = self.task
        assert task is not None
        tid = task["id"]
        if self.answer is not None:
            answer, self.answer = self.answer, None
            await ctx.update_task(tid, status="in_progress", note=f"Got it, {answer['author_name']}: {answer['body']}")
            return
        if self.progress == 0:
            await ctx.update_task(tid, status="in_progress", note=f"Starting: {task['title']}")
            self.progress = 1
            if "[ask]" in f"{task['title']} {task['description']}":
                question = await ctx.ask_human(f"Should I go ahead with '{task['title']}'?", task_id=tid)
                self.waiting_on = question["id"]
            return
        if self.progress <= self.work_steps:
            await ctx.post_message(f"Step {self.progress}/{self.work_steps} done.", task_id=tid)
            self.progress += 1
            return
        await ctx.update_task(
            tid, status="completed", output_links=[f"echo://tasks/{tid}/result"], note=f"Done: echoed '{task['title']}'."
        )
        self.completed.append(tid)
        self.task = None
