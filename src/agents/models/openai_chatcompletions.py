from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from openai import NOT_GIVEN, AsyncOpenAI, AsyncStream
from openai.types import ChatModel
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.responses import Response

from .. import _debug
from ..agent_output import AgentOutputSchema
from ..handoffs import Handoff
from ..items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from ..logger import logger
from ..tool import Tool
from ..tracing import generation_span
from ..tracing.span_data import GenerationSpanData
from ..tracing.spans import Span
from ..usage import Usage
from ..version import __version__
from .chatcmpl_converter import Converter
from .chatcmpl_stream_handler import ChatCmplStreamHandler
from .fake_id import FAKE_RESPONSES_ID
from .interface import Model, ModelTracing

if TYPE_CHECKING:
    from ..model_settings import ModelSettings


_USER_AGENT = f"Agents/Python {__version__}"
_HEADERS = {"User-Agent": _USER_AGENT}


class OpenAIChatCompletionsModel(Model):
    def __init__(
        self,
        model: str | ChatModel,
        openai_client: AsyncOpenAI,
    ) -> None:
        self.model = model
        self._client = openai_client

    def _non_null_or_not_given(self, value: Any) -> Any:
        return value if value is not None else NOT_GIVEN

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchema | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None,
    ) -> ModelResponse:
        with generation_span(
            model=str(self.model),
            model_config=dataclasses.asdict(model_settings)
            | {"base_url": str(self._client.base_url)},
            disabled=tracing.is_disabled(),
        ) as span_generation:
            response = await self._fetch_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                span_generation,
                tracing,
                stream=False,
            )

            if _debug.DONT_LOG_MODEL_DATA:
                logger.debug("Received model response")
            else:
                logger.debug(
                    f"LLM resp:\n{json.dumps(response.choices[0].message.model_dump(), indent=2)}\n"
                )

            usage = (
                Usage(
                    requests=1,
                    input_tokens=response.usage.prompt_tokens,
                    output_tokens=response.usage.completion_tokens,
                    total_tokens=response.usage.total_tokens,
                )
                if response.usage
                else Usage()
            )
            if tracing.include_data():
                span_generation.span_data.output = [response.choices[0].message.model_dump()]
            span_generation.span_data.usage = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }

            items = Converter.message_to_output_items(response.choices[0].message)

            return ModelResponse(
                output=items,
                usage=usage,
                response_id=None,
            )

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchema | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """
        Yields a partial message as it is generated, as well as the usage information.
        """
        with generation_span(
            model=str(self.model),
            model_config=dataclasses.asdict(model_settings)
            | {"base_url": str(self._client.base_url)},
            disabled=tracing.is_disabled(),
        ) as span_generation:
            response, stream = await self._fetch_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                span_generation,
                tracing,
                stream=True,
            )

            final_response: Response | None = None
            async for chunk in ChatCmplStreamHandler.handle_stream(response, stream):
                yield chunk

                if chunk.type == "response.completed":
                    final_response = chunk.response

            if tracing.include_data() and final_response:
                span_generation.span_data.output = [final_response.model_dump()]

            if final_response and final_response.usage:
                span_generation.span_data.usage = {
                    "input_tokens": final_response.usage.input_tokens,
                    "output_tokens": final_response.usage.output_tokens,
                }

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchema | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: Literal[True],
    ) -> tuple[Response, AsyncStream[ChatCompletionChunk]]: ...

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchema | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: Literal[False],
    ) -> ChatCompletion: ...

    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchema | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: bool = False,
    ) -> ChatCompletion | tuple[Response, AsyncStream[ChatCompletionChunk]]:
        converted_messages = Converter.items_to_messages(input)

        if system_instructions:
            converted_messages.insert(
                0,
                {
                    "content": system_instructions,
                    "role": "system",
                },
            )
        if tracing.include_data():
            span.span_data.input = converted_messages

        parallel_tool_calls = (
            True
            if model_settings.parallel_tool_calls and tools and len(tools) > 0
            else False
            if model_settings.parallel_tool_calls is False
            else NOT_GIVEN
        )
        tool_choice = Converter.convert_tool_choice(model_settings.tool_choice)
        response_format = Converter.convert_response_format(output_schema)

        converted_tools = [Converter.tool_to_openai(tool) for tool in tools] if tools else []

        for handoff in handoffs:
            converted_tools.append(Converter.convert_handoff_tool(handoff))

        if _debug.DONT_LOG_MODEL_DATA:
            logger.debug("Calling LLM")
        else:
            logger.debug(
                f"{json.dumps(converted_messages, indent=2)}\n"
                f"Tools:\n{json.dumps(converted_tools, indent=2)}\n"
                f"Stream: {stream}\n"
                f"Tool choice: {tool_choice}\n"
                f"Response format: {response_format}\n"
            )

        reasoning_effort = model_settings.reasoning.effort if model_settings.reasoning else None
        store = _Helpers.get_store_param(self._get_client(), model_settings)

        stream_options = _Helpers.get_stream_options_param(
            self._get_client(), model_settings, stream=stream
        )

        ret = await self._get_client().chat.completions.create(
            model=self.model,
            messages=converted_messages,
            tools=converted_tools or NOT_GIVEN,
            temperature=self._non_null_or_not_given(model_settings.temperature),
            top_p=self._non_null_or_not_given(model_settings.top_p),
            frequency_penalty=self._non_null_or_not_given(model_settings.frequency_penalty),
            presence_penalty=self._non_null_or_not_given(model_settings.presence_penalty),
            max_tokens=self._non_null_or_not_given(model_settings.max_tokens),
            tool_choice=tool_choice,
            response_format=response_format,
            parallel_tool_calls=parallel_tool_calls,
            stream=stream,
            stream_options=self._non_null_or_not_given(stream_options),
            store=self._non_null_or_not_given(store),
            reasoning_effort=self._non_null_or_not_given(reasoning_effort),
            extra_headers=_HEADERS,
            extra_query=model_settings.extra_query,
            extra_body=model_settings.extra_body,
            metadata=self._non_null_or_not_given(model_settings.metadata),
        )

        if isinstance(ret, ChatCompletion):
            return ret

        response = Response(
            id=FAKE_RESPONSES_ID,
            created_at=time.time(),
            model=self.model,
            object="response",
            output=[],
            tool_choice=cast(Literal["auto", "required", "none"], tool_choice)
            if tool_choice != NOT_GIVEN
            else "auto",
            top_p=model_settings.top_p,
            temperature=model_settings.temperature,
            tools=[],
            parallel_tool_calls=parallel_tool_calls or False,
            reasoning=model_settings.reasoning,
        )
        return response, ret

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI()
        return self._client


class _Helpers:
    @classmethod
    def is_openai(cls, client: AsyncOpenAI):
        return str(client.base_url).startswith("https://api.openai.com")

    @classmethod
    def get_store_param(cls, client: AsyncOpenAI, model_settings: ModelSettings) -> bool | None:
        # Match the behavior of Responses where store is True when not given
        default_store = True if cls.is_openai(client) else None
        return model_settings.store if model_settings.store is not None else default_store

    @classmethod
    def get_stream_options_param(
        cls, client: AsyncOpenAI, model_settings: ModelSettings, stream: bool
    ) -> dict[str, bool] | None:
        if not stream:
            return None

        default_include_usage = True if cls.is_openai(client) else None
        include_usage = (
            model_settings.include_usage
            if model_settings.include_usage is not None
            else default_include_usage
        )
        stream_options = {"include_usage": include_usage} if include_usage is not None else None
        return stream_options
