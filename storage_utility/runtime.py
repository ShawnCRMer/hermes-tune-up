"""Profile-qualified handle to the host's registered plugin context; never credentials."""
from pathlib import Path
import threading

_lock = threading.RLock()
_contexts = {}
_attempted = set()


def register_context(ctx):
    from hermes_constants import get_hermes_home
    home = str(Path(get_hermes_home()).absolute())
    ctx.register_auxiliary_task('storage_advisor', display_name='Storage Advisor',
        description='Explain a bounded nonsecret storage metadata snapshot; never authorize deletion.')
    with _lock:
        _contexts[home] = ctx
    def release():
        with _lock:
            if _contexts.get(home) is ctx:
                _contexts.pop(home, None)
    ctx.on_unload(release)


def context_for(home, *, discover=False):
    key = str(Path(home).absolute())
    with _lock:
        context = _contexts.get(key)
        first = discover and key not in _attempted
        if first:
            _attempted.add(key)
    if context is None and first:
        # The real enabled/trusted PluginManager owns registration. No synthetic
        # context, model override, environment credentials or global profile swap.
        try:
            from hermes_cli.plugins import discover_plugins
            discover_plugins()
        except Exception:
            return None
        with _lock:
            context = _contexts.get(key)
    return context


def probe(home):
    context = context_for(home, discover=True)
    if context is None:
        return {'attempted': True, 'context': False, 'registered': False,
                'authorization': False, 'reason': 'enabled plugin context is not registered in this HTTP runtime'}
    try:
        callable_route = callable(context.llm.acomplete)
    except Exception:
        callable_route = False
    return {'attempted': True, 'context': True, 'registered': True,
            'authorization': callable_route, 'modelReadiness': 'not tested until explicit generation',
            'reason': 'registered host task; host enforces trust and model readiness on invocation'}
