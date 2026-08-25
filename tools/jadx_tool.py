import subprocess
from pathlib import Path


JADX_PATH = Path(
    r"C:\Users\pcybb\Downloads\jadx-1.5.6\bin\jadx.bat"
)


def decompile_apk(apk_path: str, output_dir: str):
    """
    APK 파일을 JADX를 이용해 Java-like 소스코드로 디컴파일한다.
    """

    apk = Path(apk_path)
    output = Path(output_dir)

    # 1. APK 파일이 실제로 존재하는지 확인
    if not apk.exists():
        return {
            "success": False,
            "error": f"APK 파일이 존재하지 않습니다: {apk}"
        }

    # 2. 확장자가 .apk인지 확인
    if apk.suffix.lower() != ".apk":
        return {
            "success": False,
            "error": "입력 파일이 APK가 아닙니다."
        }

    # 3. JADX 실행 파일이 실제로 존재하는지 확인
    if not JADX_PATH.exists():
        return {
            "success": False,
            "error": f"JADX를 찾을 수 없습니다: {JADX_PATH}"
        }

    # 4. 결과 저장 폴더 생성
    output.mkdir(
        parents=True,
        exist_ok=True
    )

    # 5. JADX 실행 명령 만들기
    command = [
        str(JADX_PATH),
        "-d",
        str(output),
        str(apk)
    ]

    print(f"[+] APK: {apk}")
    print("[+] JADX 디컴파일 시작")

    # 6. JADX 실행
    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    # 7. 실행 실패 확인
    if result.returncode != 0:
        return {
            "success": False,
            "error": result.stderr
        }

    # 8. 생성된 Java-like 파일 개수 확인
    java_files = list(
        output.rglob("*.java")
    )

    print("[+] JADX 디컴파일 완료")

    return {
        "success": True,
        "apk": str(apk),
        "output": str(output),
        "java_file_count": len(java_files)
    }