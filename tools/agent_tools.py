import json
from pathlib import Path


ANALYSIS_ROOT = None

TEXT_EXTENSIONS = {
    ".java",
    ".xml",
    ".json",
    ".txt",
    ".properties",
    ".html",
    ".htm",
    ".js",
    ".css",
    ".yml",
    ".yaml",
}


def configure_analysis_root(output_dir: str):
    """Agent가 탐색할 JADX 결과 디렉터리를 설정한다."""
    global ANALYSIS_ROOT
    ANALYSIS_ROOT = Path(output_dir).resolve()


def _get_root() -> Path:
    if ANALYSIS_ROOT is None:
        raise RuntimeError("분석 루트가 설정되지 않았습니다.")

    return ANALYSIS_ROOT


def _safe_path(relative_path: str) -> Path:
    """
    Agent가 JADX 결과 폴더 밖의 파일을 읽지 못하도록 제한한다.
    """
    root = _get_root()
    target = (root / relative_path).resolve()

    if target != root and root not in target.parents:
        raise ValueError("분석 디렉터리 밖의 파일에는 접근할 수 없습니다.")

    return target


def get_apk_overview() -> str:
    """
    JADX로 디컴파일된 APK 전체 구조를 조사한다.
    분석 시작 시 사용하며 Java, XML, Web 리소스, native library 등의
    개수와 주요 Android 보안 관련 파일을 반환한다.
    """

    print("[AGENT TOOL] get_apk_overview()")

    root = _get_root()

    extension_counts = {}
    important_files = []

    important_names = {
        "androidmanifest.xml",
        "network_security_config.xml",
        "file_paths.xml",
        "backup_rules.xml",
        "data_extraction_rules.xml",
        "strings.xml",
    }

    total_files = 0

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        total_files += 1

        extension = path.suffix.lower() or "<no_extension>"

        extension_counts[extension] = (
            extension_counts.get(extension, 0) + 1
        )

        relative = path.relative_to(root).as_posix()

        if path.name.lower() in important_names:
            important_files.append(relative)

    preprocess = {}

    preprocess_path = root / "preprocess_result.json"

    if preprocess_path.exists():
        try:
            data = json.loads(
                preprocess_path.read_text(encoding="utf-8")
            )

            preprocess = {
                "statistics": data.get("statistics", {}),
                "security_candidates": data.get(
                    "security_candidates", []
                )[:20]
            }

        except Exception:
            preprocess = {
                "warning": "preprocess_result.json 읽기 실패"
            }

    result = {
        "root": str(root),
        "total_files": total_files,
        "extension_counts": extension_counts,
        "important_android_files": important_files,
        "preprocess_hints": preprocess,
    }

    return json.dumps(
        result,
        ensure_ascii=False,
        indent=2
    )


def read_manifest() -> str:
    """
    AndroidManifest.xml 전체 내용을 읽는다.
    permission, exported component, intent-filter,
    application 설정 등을 분석할 때 사용한다.
    """

    print("[AGENT TOOL] read_manifest()")

    root = _get_root()

    manifest = root / "resources" / "AndroidManifest.xml"

    if not manifest.exists():
        candidates = list(
            root.rglob("AndroidManifest.xml")
        )

        if not candidates:
            return "AndroidManifest.xml을 찾지 못했습니다."

        manifest = candidates[0]

    return manifest.read_text(
        encoding="utf-8",
        errors="ignore"
    )


def list_files(
    extension: str = "",
    path_contains: str = "",
    limit: int = 100
) -> str:
    """
    APK 내부 파일 목록을 검색한다.

    Args:
        extension: java, xml, js, so 같은 확장자 필터.
        path_contains: 경로에 포함되어야 할 문자열.
        limit: 최대 반환 파일 수.
    """

    print(
        f"[AGENT TOOL] list_files("
        f"extension={extension}, "
        f"path_contains={path_contains})"
    )

    root = _get_root()

    extension = extension.strip().lower()

    if extension and not extension.startswith("."):
        extension = "." + extension

    path_contains = path_contains.lower()

    limit = max(1, min(limit, 200))

    results = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        relative = path.relative_to(root).as_posix()

        if extension:
            if path.suffix.lower() != extension:
                continue

        if path_contains:
            if path_contains not in relative.lower():
                continue

        results.append(relative)

        if len(results) >= limit:
            break

    return json.dumps(
        {
            "count": len(results),
            "files": results
        },
        ensure_ascii=False,
        indent=2
    )


def search_text(
    query: str,
    extension: str = "",
    limit: int = 30
) -> str:
    """
    APK의 Java, XML, JS, JSON 등 텍스트 파일 전체에서
    문자열 또는 API 사용 흔적을 검색한다.

    Args:
        query: 찾을 문자열.
        extension: java, xml 등의 선택적 확장자 필터.
        limit: 최대 검색 결과 수.
    """

    print(
        f"[AGENT TOOL] search_text("
        f"query={query}, extension={extension})"
    )

    root = _get_root()

    query_lower = query.lower()

    extension = extension.strip().lower()

    if extension and not extension.startswith("."):
        extension = "." + extension

    limit = max(1, min(limit, 100))

    matches = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue

        if extension and path.suffix.lower() != extension:
            continue

        # 지나치게 큰 파일은 prototype에서는 제외
        if path.stat().st_size > 2_000_000:
            continue

        try:
            lines = path.read_text(
                encoding="utf-8",
                errors="ignore"
            ).splitlines()

        except Exception:
            continue

        for line_number, line in enumerate(lines, start=1):

            if query_lower in line.lower():

                matches.append({
                    "file": path.relative_to(root).as_posix(),
                    "line": line_number,
                    "snippet": line.strip()[:500]
                })

                if len(matches) >= limit:
                    return json.dumps(
                        {
                            "query": query,
                            "matches": matches
                        },
                        ensure_ascii=False,
                        indent=2
                    )

    return json.dumps(
        {
            "query": query,
            "matches": matches
        },
        ensure_ascii=False,
        indent=2
    )


def read_file(
    relative_path: str,
    max_chars: int = 20000
) -> str:
    """
    APK 내부의 특정 텍스트 파일 내용을 읽는다.
    search_text나 list_files 결과에서 얻은 경로를 사용한다.

    Args:
        relative_path: JADX output 기준 상대 경로.
        max_chars: 한 번에 읽을 최대 문자 수.
    """

    print(
        f"[AGENT TOOL] read_file({relative_path})"
    )

    path = _safe_path(relative_path)

    if not path.exists():
        return f"파일을 찾을 수 없습니다: {relative_path}"

    if not path.is_file():
        return f"파일이 아닙니다: {relative_path}"

    if path.suffix.lower() not in TEXT_EXTENSIONS:
        return (
            "현재 prototype에서는 binary 파일 내용을 "
            "직접 읽지 않습니다."
        )

    max_chars = max(
        1000,
        min(max_chars, 40000)
    )

    content = path.read_text(
        encoding="utf-8",
        errors="ignore"
    )

    if len(content) > max_chars:
        content = (
            content[:max_chars]
            + "\n\n[TRUNCATED]"
        )

    return content