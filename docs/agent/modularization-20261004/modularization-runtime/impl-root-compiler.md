> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

# impl-root-compiler : CandidateCompiler binding-table wiring

## 1. actual source changes (stage 1)
file: src/redteam/orchestration/candidates/compiler.py only (tool_selection.py untouched, no hardcoded ToolId)
- imports: Mapping + TYPE_CHECKING OperationTargetBinding only, no capability import
- init: (catalog=None, *, operation_bindings=None) -> self._operation_bindings; legacy direct call keeps builtin behavior
- _bind_target_arguments static kept, bindings=None appended at end; positional private calls preserved
- validate_network_arguments x2 both pass bindings=self._operation_bindings (raw args + bound args)
- catalog ValueError -> (no_compatible_tool, empty) at list_tools_for_action, list_by_capability, get consistency
- bound tool_id normalization already canonical via tool.tool_id; raw evidence untouched

## 2. root caller API - 4 lines (engine.py already done)
engine.py 1871-1873 ready, no edit needed:
  1: compiled, rejected = CandidateCompiler(
  2:     self.deps.tool_catalog, operation_bindings=self.deps.operation_bindings
  3: ).compile_drafts_with_rejections(drafts,)
  4: RuntimeDeps.operation_bindings exists context.py:99 dict default; app assembly fills from builtin table

## 3. stage 2: helper + argument_bindings wiring (this turn)
files: orchestration/candidates/compiler.py + orchestration/target_binding.py only; core untouched; no tests/lint run
- core operation_target_binding(operation, bindings) now real: both files delegate to it, all literal
  web_search/endpoint_discovery/page_read fallbacks removed (compiler inline row lookup + target_binding _binding_flag)
- compiler bind step fills provider-declared remote names from existing canonical target values:
  only schema-declared args, explicit args win, never invents (None skipped); known-URL path also fills
  required mapped path names, not just literal path
- compiler bound validation passes the chosen tool argument_bindings; raw validation stays standard-only
  because the tool is unresolved at raw stage (resolution happens after raw check)
- target_binding validate_network_arguments gains optional argument_bindings: mapped port/host/url args get
  the same identity checks as their canonical counterparts (standard checks unchanged); alias-vs-canonical
  conflicts rejected with field-names-only messages; alias port counts toward pinned-port requirement;
  non-network shared values (path, asset_ref, ...) left to the tool schema; mapping declares names only,
  it grants no extra authority to the proposer
