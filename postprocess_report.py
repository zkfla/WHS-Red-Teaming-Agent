"""
Codex 실행이 끝난 뒤, MCP 서버가 기록해둔 도구 호출 로그와 Codex의 최종
리포트 텍스트를 대조해서 audit_report로 재검수한다.

사용법:
    python3 postprocess_report.py <codex_report.txt>
"""

import sys
import os
import re
import json

WORKDIR = "./agent_workdir"
LOG_PATH = os.path.join(WORKDIR, "mcp_tool_log.jsonl")


def extract_exported_components(manifest_xml: str) -> dict:
    result = {"activity": [], "receiver": [], "provider": []}
    for tag in result.keys():
        for block in re.findall(rf"<{tag}\b[^>]*>", manifest_xml):
            if not re.search(r'exported\s*=\s*["\']true["\']', block):
                continue
            m = re.search(r'name\s*=\s*["\']([^"\']+)["\']', block)
            if m:
                full = m.group(1)
                short = full.rsplit(".", 1)[-1] if "." in full else full
                if short:
                    result[tag].append(short)
    return result


def audit_report(report_text: str, tool_calls: list, exported: dict = None) -> str:
    activity_matched = {}
    activity_attempted = set()
    provider_has_data = {}

    for name, args, result in tool_calls:
        result_str = str(result).strip()
        if name == "launch_activity":
            component = args.get("component", "")
            short_name = component.split("/")[-1].lstrip(".")
            if short_name:
                activity_attempted.add(short_name)
                is_match = result_str.startswith("MATCH:")
                activity_matched[short_name] = activity_matched.get(short_name, False) or is_match
        elif name == "query_content_provider":
            uri = args.get("uri", "")
            is_data = result_str.startswith("DATA RETURNED:")
            provider_has_data[uri] = provider_has_data.get(uri, False) or is_data
        elif name == "insert_content_provider":
            uri = args.get("uri", "")
            is_insert_ok = result_str.startswith("INSERT_SUCCEEDED:")
            provider_has_data[uri] = provider_has_data.get(uri, False) or is_insert_ok

    warnings = []
    for line in report_text.splitlines():
        if "[Verified]" not in line:
            continue
        for short_name, matched in activity_matched.items():
            if short_name and short_name in line and not matched:
                warnings.append(
                    f"'{short_name}'가 [Verified]로 적혀 있지만, 실제 launch_activity "
                    f"결과는 MATCH가 아니었습니다. 해당 줄: {line.strip()}"
                )
        if exported:
            for exp_name in exported.get("activity", []):
                if exp_name and exp_name in line and exp_name not in activity_attempted:
                    warnings.append(
                        f"'{exp_name}'가 [Verified]로 적혀 있지만, launch_activity가 "
                        f"단 한 번도 호출되지 않았습니다. 해당 줄: {line.strip()}"
                    )
        for uri, has_data in provider_has_data.items():
            uri_tail = uri.split("/")[-2] if "/" in uri else uri
            if uri_tail and uri_tail in line and not has_data:
                warnings.append(
                    f"ContentProvider 관련 줄이 [Verified]로 적혀 있지만, 실제 결과는 "
                    f"DATA RETURNED/INSERT_SUCCEEDED가 아니었습니다. 해당 줄: {line.strip()}"
                )

    coverage_notes = []
    if exported:
        untested_activities = [n for n in exported.get("activity", []) if n and n not in activity_attempted]
        if untested_activities:
            coverage_notes.append(
                f"매니페스트에서 exported activity로 확인됐지만 launch_activity로 "
                f"한 번도 시도되지 않은 것: {', '.join(sorted(set(untested_activities)))}"
            )

    if warnings:
        report_text += "\n\n---\n### ⚠️ 자동 재검수 경고 (기계적 사실 대조 결과)\n"
        report_text += "\n".join(f"- {w}" for w in warnings)
    if coverage_notes:
        report_text += "\n\n---\n### 📋 커버리지 점검 (기계적 계산)\n"
        report_text += "\n".join(f"- {c}" for c in coverage_notes)
    return report_text


def main():
    if len(sys.argv) != 2:
        print("사용법: python3 postprocess_report.py <codex_report.txt>")
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        report_text = f.read()

    tool_calls = []
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                tool_calls.append((entry["name"], entry["args"], entry["result"]))
    else:
        print(f"[경고] 도구 호출 로그({LOG_PATH})를 찾을 수 없습니다. 커버리지 체크 없이 진행합니다.")

    manifest_text = next((str(r) for n, a, r in tool_calls if n == "extract_manifest"), "")
    exported = extract_exported_components(manifest_text) if manifest_text else {}

    print(f"\n=== 실제 MCP 도구 호출 내역 ({len(tool_calls)}개) ===")
    for name, args, result in tool_calls:
        print(f"[호출] {name}({args})")
        print(f"[결과] {str(result)[:300]}...")

    final = audit_report(report_text, tool_calls, exported)
    print("\n=== 최종 재검수된 리포트 ===")
    print(final)


if __name__ == "__main__":
    main()
