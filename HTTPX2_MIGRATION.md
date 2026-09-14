# HTTPX2 migration spec

## Objective

LiteLLM's own runtime HTTP code uses `httpx2`, while legacy `httpx` remains only where a dependency still requires it. This is a major-version migration: callers that pass LiteLLM-managed HTTP clients must construct them from `httpx2`.

## Decisions

* LiteLLM-owned imports are `import httpx2 as httpx`, `from httpx2 import ...`, or imports through `litellm.litellm_core_utils.httpx2_compat`.
* `httpx2.alias_httpx()` is not called. It changes the meaning of `import httpx` for a whole process, which is unsafe for a library and too late if a host application imported OpenAI first.
* OpenAI is upgraded to 3.x because OpenAI 2.x creates and checks legacy `httpx` clients.
* The experimental MCP client remains on MCP 1.x and legacy `httpx`. MCP 1.x accepts legacy clients and exceptions, so this boundary is kept intact until MCP is migrated.
* Tests that inject clients and inspect HTTP exceptions use `httpx2`. RESPX route declarations remain on its legacy `httpx` API; `tests/test_litellm/httpx2_respx.py` is the default test mocker, keeps that declaration API, and routes HTTPX2 responses through conversion while leaving legacy MCP clients on their native path.

## Compatibility module

`litellm/litellm_core_utils/httpx2_compat.py` centralizes the private HTTPX2 imports, environment proxy parsing, type aliases, and boundary checks. Runtime code should not import `httpx2._*` directly.

## Dependency and validation boundaries

The base dependency is `httpx2 >= 2.7,< 3`. OpenAI is `>= 3.13,< 4`. The lock still contains legacy `httpx` because MCP 1.x, HTTPX-SSE, FastAPI SSO, test tooling, and other dependencies require it. Those dependencies must not receive `httpx2` clients.

Public behavior changes:

* A user-created `httpx.AsyncClient` can no longer be passed where LiteLLM's handler contract is `httpx2.AsyncClient`.
* Default SSL verification now uses the operating system trust store through HTTPX2's `truststore` dependency.
* HTTPX loggers are `httpx2` and `httpcore2`, not `httpx` and `httpcore`.

## Verification

The migration is validated by package import checks, `compileall`, Ruff, basedpyright on the compatibility module, the HTTP handler test suites, and the full unit suite with the RESPX compatibility layer. Full CI requires fixing the local Rust toolchain mirror before package build.
