"""Compile a declarative submission directory into a Google ADK agent tree.

A local stand-in for the official `adk-submission` compile_submission(), which
is not public. It never imports code from the submission: YAML is loaded with
the same sandboxed `!include` loader the validator uses, and tool names resolve
only against the harness tool functions passed in.

Model backends:
  gemini  - Gemini API `gemma-4-31b-it` (bf16 weights, not the QAT build).
            It rejects thinking_config, so that field is dropped; the model
            still thinks by default. Needs GOOGLE_API_KEY. The free tier caps
            input at 16k tokens per minute, so any longer prompt is impossible.
  openai  - Any OpenAI-compatible endpoint through LiteLlm, which is how the
            official harness reaches its local vLLM server. Pass api_base and
            model name; the key comes from the environment variable named by
            api_key_env. ADK sends tool results with role "tool_responses" for
            any model named like gemma-4 (vLLM's Gemma 4 chat template expects
            it, so the official harness does this too). Servers that only accept
            standard OpenAI roles (e.g. NVIDIA NIM) need standard_tool_role=True.

Not yet supported locally: LoRA adapters (no local serving) and skills.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from google.adk.agents import BaseAgent, LlmAgent, LoopAgent, ParallelAgent, SequentialAgent
from google.adk.models.google_llm import Gemini
from google.adk.models import lite_llm as lite_llm_module
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from scripts.validate_submission import ROOT_CONFIG_NAMES, load_yaml, validate

log = logging.getLogger(__name__)

GEMINI_MODEL = "gemma-4-31b-it"
GENERATION_FIELDS = {
    "temperature", "top_p", "top_k", "max_output_tokens", "presence_penalty",
    "frequency_penalty", "stop_sequences", "seed", "response_mime_type",
}


class SubmissionCompiler:
    def __init__(
        self,
        submission_dir: Path,
        tools: dict[str, Callable[..., str]],
        backend: str = "gemini",
        api_base: str | None = None,
        model_name: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        standard_tool_role: bool = False,
    ) -> None:
        self.root = submission_dir.resolve()
        self.tools = tools
        self.backend = backend
        if backend == "gemini":
            self.model = Gemini(
                model=model_name or GEMINI_MODEL,
                # Mirrors the official ModelRetryPlugin (5 attempts, 2s..60s backoff).
                retry_options=types.HttpRetryOptions(
                    attempts=5, initial_delay=2.0, max_delay=60.0, exp_base=2.0, jitter=0.2,
                    http_status_codes=[429, 500, 502, 503, 504],
                ),
            )
        elif backend == "openai":
            if not api_base or not model_name:
                raise ValueError("openai backend needs api_base and model_name")
            api_key = os.environ.get(api_key_env)
            if not api_key:
                raise ValueError(f"environment variable {api_key_env} is not set")
            if standard_tool_role:
                # Process-wide switch of ADK's Gemma 4 detection; only used for local
                # runs against non-vLLM endpoints, never for the submitted config.
                lite_llm_module._is_gemma4_model = lambda model: False
            self.model = LiteLlm(model=f"openai/{model_name}", api_base=api_base, api_key=api_key, num_retries=5)
        else:
            raise ValueError(f"unsupported backend {backend!r}")
        self.dropped: set[str] = set()

    def compile(self) -> BaseAgent:
        errors = validate(self.root)
        if errors:
            raise ValueError("invalid submission:\n  " + "\n  ".join(errors))
        root_file = next(self.root / n for n in ROOT_CONFIG_NAMES if (self.root / n).is_file())
        return self._agent(load_yaml(root_file, self.root), root_file.parent)

    def _load_ref(self, ref: dict[str, Any], base_dir: Path) -> tuple[dict[str, Any], Path]:
        path = (base_dir / ref["config_path"]).resolve()
        return load_yaml(path, self.root), path.parent

    def _generation_config(self, cfg: dict[str, Any] | None) -> types.GenerateContentConfig | None:
        if not cfg:
            return None
        allowed = GENERATION_FIELDS | ({"thinking_config"} if self.backend == "openai" else set())
        self.dropped |= set(cfg) - allowed
        kept = {k: v for k, v in cfg.items() if k in allowed}
        if isinstance(kept.get("thinking_config"), dict) and kept["thinking_config"].get("thinking_level"):
            kept["thinking_config"] = {**kept["thinking_config"], "thinking_level": kept["thinking_config"]["thinking_level"].upper()}
        return types.GenerateContentConfig(**kept)

    def _tool(self, entry: Any, base_dir: Path) -> Any:
        if isinstance(entry, str):
            return self.tools[entry]
        if isinstance(entry, dict) and "agent_tool" in entry:
            ref = entry["agent_tool"]
            config, sub_dir = self._load_ref(ref, base_dir) if "config_path" in ref else (ref, base_dir)
            return AgentTool(agent=self._agent(config, sub_dir), skip_summarization=bool(ref.get("skip_summarization", False)))
        return AgentTool(agent=self._agent(entry, base_dir))

    def _agent(self, cfg: dict[str, Any], base_dir: Path) -> BaseAgent:
        subs = []
        for ref in cfg.get("sub_agents") or []:
            sub_cfg, sub_dir = self._load_ref(ref, base_dir) if "config_path" in ref else (ref, base_dir)
            subs.append(self._agent(sub_cfg, sub_dir))
        common = {"name": cfg["name"], "description": cfg.get("description", ""), "sub_agents": subs}

        agent_class = cfg.get("agent_class", "LlmAgent")
        if agent_class == "SequentialAgent":
            return SequentialAgent(**common)
        if agent_class == "ParallelAgent":
            return ParallelAgent(**common)
        if agent_class == "LoopAgent":
            return LoopAgent(**common, max_iterations=cfg.get("max_iterations", 500))

        if cfg.get("adapter"):
            self.dropped.add("adapter")
        if cfg.get("skills"):
            self.dropped.add("skills")
        optional = {k: cfg[k] for k in ("output_key", "include_contents", "disallow_transfer_to_parent",
                                        "disallow_transfer_to_peers", "global_instruction") if cfg.get(k) is not None}
        return LlmAgent(
            **common,
            **optional,
            model=self.model,
            instruction=cfg.get("instruction", ""),
            tools=[self._tool(t, base_dir) for t in cfg.get("tools") or []],
            generate_content_config=self._generation_config(cfg.get("generate_content_config")),
        )


def compile_submission(submission_dir: Path, tools: dict[str, Callable[..., str]], **backend: Any) -> BaseAgent:
    """Compile with the given backend options (see SubmissionCompiler)."""
    compiler = SubmissionCompiler(submission_dir, tools, **backend)
    agent = compiler.compile()
    if compiler.dropped:
        log.warning("local backend ignores these submission fields: %s", sorted(compiler.dropped))
    return agent
