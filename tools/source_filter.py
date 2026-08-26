from pathlib import Path


def collect_app_sources(
    source_root: str,
    package_name: str
):
    """
    앱 패키지에 해당하는 Java-like 소스코드만 수집한다.
    """

    source_root = Path(source_root)

    if not source_root.exists():
        return {
            "success": False,
            "error": f"sources 폴더가 없습니다: {source_root}"
        }

    # com.android.insecurebankv2
    # ↓
    # com/android/insecurebankv2
    package_path = Path(
        *package_name.split(".")
    )

    app_source_dir = (
        source_root / package_path
    )

    if not app_source_dir.exists():
        return {
            "success": False,
            "error": (
                "앱 패키지에 해당하는 "
                f"소스 폴더가 없습니다: {app_source_dir}"
            )
        }

    java_files = list(
        app_source_dir.rglob("*.java")
    )

    return {
        "success": True,
        "source_dir": str(app_source_dir),
        "java_file_count": len(java_files),
        "java_files": [
            str(file)
            for file in java_files
        ]
    }