> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

## coordinator registry lifetime note (source-only, code untouched)

evidence (real leak, not hypothetical):
- coordination.py:260 `_COORDINATORS = WeakKeyDictionary()`; :279/:290 store `Coordinator` under the task store key; no eviction anywhere.
- Coordinator.__init__ (:216) holds `self.backend`; MemoryStateBackend (:40-46) and SqliteStateBackend (:99-112) hold `self._tasks = tasks` (and paths). value -> backend -> tasks -> key strong chain, so the weak key never dies and every per-run store set stays resident for the process lifetime.
- RuntimeDeps is `@dataclass(frozen=True)` (context.py:47-48) and has no coordinator field; all consumers (engine.py:665/1027/1044/2601/2615/2741, runner.py:219-429, replanning.py:582, operator_intents/service.py:312) reach the coordinator only through the global lookup.

minimal consistent fix (3 lines):
1) Add one public `coordinator: Coordinator | None` field to RuntimeDeps (strong owner, one per run) and pass it at construction; bootstrap already has the instance pre-construction (bootstrap.py:249/:299, :434-435/:576).
2) Switch runtime consumers to `deps.coordinator`; keep `coordinator_for(tasks, paths, *, backend=None)` as the legacy helper for non-runtime callers and make the global registry weak-value (deref helper), so "one backend per store" holds while the owner ref lives.
3) Never weaken the backend->store refs (a store must not be collectable mid-commit); a store set stays pinned only for the run's live owner, then drops with it. Note: a second backend for the same store re-binds `tasks._lock`/`paths._lock`, so every live execution path must hold the owner ref (transient CLI/test calls hold the return value only for their call, which is safe).

## fix re-review (after root applied the lifetime fix)

- Verified sound: weak-value registry (coordination.py:260/276-292), builtin records survive facade death via `_coordination_backend` reuse (state_backend.py:191-214), RuntimeDeps.coordinator field (context.py:96) + both builders pass a local (bootstrap.py:263/336, :631), consumers prefer deps.coordinator (engine 6 sites, runner 6 sites). No globals hold the store/backend strongly; store<->backend cycle is gc-collectable.
- One hole: bootstrap build_application :449-450 discards the injection result, so the weakref entry dies before :451 and validate_configuration's namespace has no coordinator -> legacy selection runs: custom store types raise CoordinationError (injection unusable), builtin types silently bypass the injected backend. Fix (matches the :263 pattern): `coordinator = coordinator_for(resolved_tasks, resolved_paths, backend=atomic_state_backend)` then `coordinator.backend.check_history(resolved_intents)` (preserves the history pairing check previously done by validate_configuration).
- Documented edge, not blocking: an unowned legacy caller that injects a custom backend, drops the coordinator and later calls the plain helper cannot be distinguished; real runs are covered because RuntimeDeps holds the owner for the run.
