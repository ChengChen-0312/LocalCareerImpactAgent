"""Schema-constrained text sampling from the already resident Qwen VL model.

The worker receives extracted text only. MLX-LM supplies the token-mask hook that
MLX-VLM 0.3.7 lacks; the adapter reuses the same VL language model and weights.
The existing worker decoder and all semantic validators still check the result.
"""

from __future__ import annotations

from typing import Any

from .protocol import GenerateRequest, GenerationMetrics


class StructuredTextDecoder:
    def __init__(self, model: Any, processor: Any) -> None:
        import mlx.nn as nn
        from llguidance.hf import from_tokenizer
        from mlx_lm.tokenizer_utils import TokenizerWrapper

        class TextModel(nn.Module):
            def __init__(self, language_model: Any) -> None:
                super().__init__()
                self.language_model = language_model

            @property
            def layers(self):
                return self.language_model.layers

            def __call__(self, tokens, cache=None):
                return self.language_model(tokens, cache=cache).logits

        tokenizer = processor.tokenizer if hasattr(processor, "tokenizer") else processor
        eos = getattr(tokenizer, "eos_token_ids", None) or [tokenizer.eos_token_id]
        if isinstance(eos, int):
            eos = [eos]
        self._model = TextModel(model.language_model)
        self._tokenizer = TokenizerWrapper(tokenizer, eos_token_ids=eos)
        self._grammar_tokenizer = from_tokenizer(
            tokenizer, n_vocab=model.config.text_config.vocab_size, eos_token=list(eos),
        )

    def generate(self, request: GenerateRequest, prompt: str) -> tuple[str, GenerationMetrics]:
        import mlx.core as mx
        import numpy as np
        from llguidance import LLMatcher
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_logits_processors, make_sampler

        grammar = LLMatcher.grammar_from_json_schema(
            request.response_schema,
            overrides={
                "whitespace_flexible": True,
                # Keep normal pretty-printed JSON while bounding formatting cost.
                "whitespace_pattern": r"[\x20\x0A\x0D\x09]{1,16}",
                "lenient": False,
            },
        )
        matcher = LLMatcher(self._grammar_tokenizer, grammar)
        started = False

        def constrain(tokens, logits):
            nonlocal started
            # MLX-LM's first callback contains prompt tokens; later callbacks end
            # with the previous sampled token. Never feed the prompt to the grammar.
            if started and not matcher.consume_token(int(tokens[-1].item())):
                raise ValueError("structured decoder rejected a sampled token")
            started = True
            allowed = np.frombuffer(matcher.compute_logit_bias(), dtype=np.uint8)
            if matcher.is_error() or len(allowed) != logits.shape[-1]:
                raise ValueError("structured decoder mask is unavailable")
            return mx.where(mx.array(allowed) != 0, logits, -mx.inf)

        processors = make_logits_processors(
            repetition_penalty=float(request.repetition_penalty),
            repetition_context_size=20,
        )
        processors.append(constrain)
        mx.random.seed(request.seed)
        parts: list[str] = []
        last = None
        try:
            for chunk in stream_generate(
                self._model,
                self._tokenizer,
                prompt,
                max_tokens=request.max_tokens,
                sampler=make_sampler(temp=float(request.temperature), top_p=float(request.top_p)),
                logits_processors=processors,
            ):
                parts.append(chunk.text)
                last = chunk
        finally:
            mx.clear_cache()
        if last is None:
            raise ValueError("structured decoder produced no result")
        return "".join(parts), GenerationMetrics(
            prompt_tokens=last.prompt_tokens,
            generation_tokens=last.generation_tokens,
            total_tokens=last.prompt_tokens + last.generation_tokens,
            generation_limit=request.max_tokens,
            reached_generation_limit=last.generation_tokens >= request.max_tokens,
        )
