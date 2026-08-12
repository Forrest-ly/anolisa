#!/usr/bin/env python3
"""Unit tests for the AgentScope SDK middleware adapter.

The real ``agentscope`` package is not required at test time; fake
``agentscope.message`` and ``agentscope.tool`` modules are injected into
``sys.modules`` before loading the adapter source.
"""

import asyncio
import importlib.util
import os
import sys
import unittest
from types import SimpleNamespace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN_SRC = os.path.join(
    _REPO_ROOT, "adapters", "tokenless", "agentscope", "__init__.py"
)


class _FakeTextBlock:
    def __init__(self, type=None, text=None, **kwargs):
        self.type = type
        self.text = text
        self._kwargs = kwargs

    def get(self, key, default=None):
        return getattr(self, key, default)


class _FakeToolResponse:
    def __init__(self, content, id=None, metadata=None, is_last=True, stream=False):
        self.content = content
        self.id = id
        self.metadata = metadata
        self.is_last = is_last
        self.stream = stream


class _FakeAgentscopeModule:
    pass


def _install_fake_agentscope():
    """Inject minimal fake agentscope modules."""
    message_mod = _FakeAgentscopeModule()
    message_mod.TextBlock = _FakeTextBlock
    tool_mod = _FakeAgentscopeModule()
    tool_mod.ToolResponse = _FakeToolResponse
    sys.modules["agentscope"] = _FakeAgentscopeModule()
    sys.modules["agentscope.message"] = message_mod
    sys.modules["agentscope.tool"] = tool_mod


def _load_plugin(name: str):
    """Load the agentscope adapter source under a unique module name."""
    sys.modules.pop("hook_utils", None)
    _install_fake_agentscope()
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_SRC)
    module = importlib.util.module_from_spec(spec)
    pre_path = sys.path[:]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = pre_path
    return module


async def _async_gen(items):
    for item in items:
        yield item


class ToolCallAccessorTest(unittest.TestCase):
    """Unit tests for the tool-call accessor helpers."""

    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin("agentscope_accessor_test")

    def test_dict_tool_call(self):
        tc = {"name": "bash", "input": {"command": "ls"}, "id": "call_1"}
        self.assertEqual(self.plugin._tool_name(tc), "bash")
        self.assertEqual(self.plugin._tool_input(tc), {"command": "ls"})
        self.assertEqual(self.plugin._tool_call_id(tc), "call_1")

    def test_object_tool_call(self):
        tc = SimpleNamespace(
            name="web_fetch", input={"url": "http://x"}, tool_use_id="tu_1"
        )
        self.assertEqual(self.plugin._tool_name(tc), "web_fetch")
        self.assertEqual(self.plugin._tool_input(tc), {"url": "http://x"})
        self.assertEqual(self.plugin._tool_call_id(tc), "tu_1")

    def test_tool_call_defaults(self):
        self.assertEqual(self.plugin._tool_name(None), "")
        self.assertEqual(self.plugin._tool_input(None), {})
        self.assertEqual(self.plugin._tool_call_id(None), "")


class ResponseBuilderTest(unittest.TestCase):
    """Unit tests for _block_response and _replace_text."""

    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin("agentscope_builder_test")

    def test_block_response(self):
        resp = self.plugin._block_response("blocked")
        self.assertIsInstance(resp, _FakeToolResponse)
        self.assertEqual(len(resp.content), 1)
        self.assertEqual(resp.content[0].text, "blocked")
        self.assertTrue(resp.is_last)

    def test_replace_text_preserves_attributes(self):
        original = _FakeToolResponse(
            content=[_FakeTextBlock("text", "old")],
            id="r1",
            metadata={"k": "v"},
            is_last=True,
            stream=False,
        )
        replaced = self.plugin._replace_text(original, "new")
        self.assertEqual(replaced.id, "r1")
        self.assertEqual(replaced.metadata, {"k": "v"})
        self.assertTrue(replaced.is_last)
        self.assertFalse(replaced.stream)
        self.assertEqual(replaced.content[0].text, "new")

    def test_replace_text_only_first_text_block(self):
        original = _FakeToolResponse(
            content=[
                _FakeTextBlock("text", "first"),
                _FakeTextBlock("text", "second"),
            ]
        )
        replaced = self.plugin._replace_text(original, "changed")
        self.assertEqual(replaced.content[0].text, "changed")
        self.assertEqual(replaced.content[1].text, "second")


class TransformResponseTest(unittest.TestCase):
    """Unit tests for _transform_response behavior."""

    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin("agentscope_transform_test")

    def _run(self, coro):
        return asyncio.run(coro)

    def test_streaming_chunk_passthrough(self):
        resp = _FakeToolResponse(
            content=[_FakeTextBlock("text", "chunk")],
            stream=True,
            is_last=False,
        )
        result = self._run(
            self.plugin._transform_response(resp, "bash", "s1", "tc1")
        )
        self.assertIs(result, resp)

    def test_short_response_passthrough(self):
        resp = _FakeToolResponse(content=[_FakeTextBlock("text", "hi")])
        self.plugin._have = lambda name, fallback: True  # noqa: ARG005
        result = self._run(
            self.plugin._transform_response(resp, "bash", "s1", "tc1")
        )
        self.assertIs(result, resp)

    def test_compression_and_toon_applied(self):
        original_text = '{"a": "' + "x" * 500 + '"}'
        resp = _FakeToolResponse(content=[_FakeTextBlock("text", original_text)])

        async def fake_compress(tool_name, result, session_id, tool_call_id):
            return "compressed"

        async def fake_toon(data, session_id="", tool_call_id=""):
            return ("toon_out", 50)

        self.plugin._compress_response = fake_compress
        self.plugin._encode_toon = fake_toon
        self.plugin._have = lambda name, fallback: True  # noqa: ARG005

        result = self._run(
            self.plugin._transform_response(resp, "api", "s1", "tc1")
        )
        self.assertEqual(result.content[0].text, "toon_out")


class MiddlewareFlowTest(unittest.TestCase):
    """End-to-end tests for tokenless_middleware."""

    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin("agentscope_flow_test")

    def _run(self, coro):
        return asyncio.run(coro)

    async def _collect(self, gen):
        return [item async for item in gen]

    def test_env_check_blocks(self):
        async def fake_env_check(tool_name):
            return "not ready"

        self.plugin._env_check = fake_env_check
        self.plugin._have = lambda name, fallback: True  # noqa: ARG005

        async def next_handler(**kwargs):
            return _async_gen(
                [_FakeToolResponse(content=[_FakeTextBlock(text="should not run")])]
            )

        gen = self.plugin.tokenless_middleware(
            {"tool_call": {"name": "npm"}, "session_id": "s1"}, next_handler
        )
        results = self._run(self._collect(gen))
        self.assertEqual(len(results), 1)
        self.assertIn("not ready", results[0].content[0].text)

    def test_rewrite_blocks(self):
        async def fake_env_check(tool_name):
            return None

        async def fake_rewrite(args, session_id, tool_call_id):
            return {"action": "block", "message": "use rtk"}

        self.plugin._env_check = fake_env_check
        self.plugin._try_rewrite = fake_rewrite
        self.plugin._have = lambda name, fallback: True  # noqa: ARG005

        async def next_handler(**kwargs):
            return _async_gen([_FakeToolResponse(content=[])])

        gen = self.plugin.tokenless_middleware(
            {
                "tool_call": {"name": "bash", "input": {"command": "ls"}},
                "session_id": "s1",
            },
            next_handler,
        )
        results = self._run(self._collect(gen))
        self.assertEqual(len(results), 1)
        self.assertIn("use rtk", results[0].content[0].text)

    def test_normal_execution_transforms(self):
        async def fake_env_check(tool_name):
            return None

        async def fake_rewrite(args, session_id, tool_call_id):
            return None

        original_text = '{"data": "' + "x" * 500 + '"}'

        async def fake_transform(response, tool_name, session_id, tool_use_id):
            return self.plugin._replace_text(response, "transformed")

        self.plugin._env_check = fake_env_check
        self.plugin._try_rewrite = fake_rewrite
        self.plugin._transform_response = fake_transform
        self.plugin._have = lambda name, fallback: True  # noqa: ARG005

        async def next_handler(**kwargs):
            return _async_gen(
                [_FakeToolResponse(content=[_FakeTextBlock(text=original_text)])]
            )

        gen = self.plugin.tokenless_middleware(
            {"tool_call": {"name": "api"}, "session_id": "s1"}, next_handler
        )
        results = self._run(self._collect(gen))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].content[0].text, "transformed")


class RegisterTest(unittest.TestCase):
    """Tests for the register() entry point."""

    def test_register_calls_toolkit(self):
        plugin = _load_plugin("agentscope_register_test")
        calls = []

        class FakeToolkit:
            def register_middleware(self, mw):
                calls.append(mw)

        plugin.register(FakeToolkit())
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0], plugin.tokenless_middleware)


if __name__ == "__main__":
    unittest.main()
