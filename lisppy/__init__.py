"""Public package facade for the LisPy runtime."""

import lisp as _runtime


__version__ = _runtime.VERSION

__all__ = [
    "VERSION",
    "LANGUAGE_PROFILE",
    "DISTRIBUTION_NAME",
    "NIL",
    "Symbol",
    "Pair",
    "Env",
    "LispError",
    "LispSyntaxError",
    "CapabilityDenied",
    "ExecutionLimitExceeded",
    "ExecutionLimits",
    "ExecutionResult",
    "LispyVM",
    "make_global_env",
    "parse",
    "read_source",
    "run_file",
    "run_string",
    "run_hosted_governor",
    "to_wire",
    "rb_post",
    "rb_comment",
    "rb_react",
    "set_state_dir",
    "run_demo",
    "contract_bundle",
    "contract_bundle_v2",
    "contract_manifest",
    "load_contract",
    "load_profile",
    "verify_contract_bundle_v2",
    "registered_source",
    "run_registered_governor",
    "run_hosted_frame",
    "run_hosted_frame_v2",
    "load_mars_contract",
    "load_mars_vectors",
    "run_mars_vectors",
    "main",
]


def __getattr__(name):
    if name == "run_demo":
        from .demo import run_demo

        return run_demo
    if name in {
        "contract_bundle",
        "contract_bundle_v2",
        "contract_manifest",
        "load_contract",
        "load_profile",
        "verify_contract_bundle_v2",
    }:
        from . import contracts

        return getattr(contracts, name)
    if name in {
        "registered_source",
        "run_registered_governor",
        "run_hosted_frame",
        "run_hosted_frame_v2",
    }:
        from . import host

        return getattr(host, name)
    if name in {
        "load_mars_contract",
        "load_mars_vectors",
        "run_mars_vectors",
    }:
        from . import mars

        return getattr(mars, name)
    if name in __all__ or name == "STATE_DIR":
        return getattr(_runtime, name)
    raise AttributeError(name)


def __dir__():
    return sorted(set(globals()) | set(__all__) | {"STATE_DIR", "__version__"})
