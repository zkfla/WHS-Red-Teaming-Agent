import json
from pathlib import Path
import subprocess
from agent.mobile_agent import run_mobile_agent

from tools.jadx_tool import decompile_apk
from tools.manifest_tool import get_package_name
from tools.source_filter import collect_app_sources
from tools.security_selector import select_security_files


def main():

    print("=== Mobile APK Red Teaming Agent Prototype ===")

    # =================================
    # APK 경로 입력
    # =================================

    apk_path = input(
        "분석할 APK 파일 경로를 입력하세요: "
    ).strip()

    # 경로에 따옴표가 같이 들어왔을 경우 제거
    apk_path = apk_path.strip('"').strip("'")

    apk = Path(apk_path)

    # APK 존재 여부 확인
    if not apk.exists():
        print("APK 파일을 찾을 수 없습니다.")
        print("입력 경로:", apk)
        return

    # InsecureBankv2.apk
    # → InsecureBankv2
    app_name = apk.stem

    # output/InsecureBankv2
    output_dir = Path("output") / app_name


    # =================================
    # STEP 1. JADX 디컴파일
    # =================================

    print()
    print("[STEP 1] APK 디컴파일")

    jadx_result = decompile_apk(
        apk_path=str(apk),
        output_dir=str(output_dir)
    )

    if not jadx_result["success"]:

        print("디컴파일 실패")
        print("오류:", jadx_result["error"])

        return

    print("디컴파일 성공")

    print(
        "전체 Java-like 파일:",
        jadx_result["java_file_count"]
    )


    # =================================
    # STEP 2. AndroidManifest.xml 분석
    # =================================

    print()
    print("[STEP 2] AndroidManifest.xml 분석")

    manifest_path = (
        output_dir
        / "resources"
        / "AndroidManifest.xml"
    )

    manifest_result = get_package_name(
        str(manifest_path)
    )

    if not manifest_result["success"]:

        print("Manifest 분석 실패")
        print("오류:", manifest_result["error"])

        return

    package_name = manifest_result["package_name"]

    print(
        "앱 패키지 이름:",
        package_name
    )


    # =================================
    # STEP 3. 앱 자체 소스코드 식별
    # =================================

    print()
    print("[STEP 3] 앱 자체 소스코드 추출")

    source_root = (
        output_dir / "sources"
    )

    source_result = collect_app_sources(
        source_root=str(source_root),
        package_name=package_name
    )

    if not source_result["success"]:

        print("앱 소스 추출 실패")
        print("오류:", source_result["error"])

        return

    java_files = source_result["java_files"]

    print(
        "앱 자체 Java-like 파일:",
        source_result["java_file_count"]
    )


    # =================================
    # STEP 4. 보안 관련 신호 탐색
    # =================================

    print()
    print("[STEP 4] 보안 관련 코드 후보 선별")

    security_files = select_security_files(
        java_files
    )

    print(
        "보안 관련 후보 파일:",
        len(security_files)
    )

    print()

    for item in security_files:

        print(
            f"- {item['file']}"
        )

        print(
            "  카테고리:",
            ", ".join(
                item["categories"]
            )
        )


    # =================================
    # STEP 5. 전처리 결과 JSON 저장
    # =================================

    print()
    print("[STEP 5] 전처리 결과 저장")

    preprocess_result = {

        "apk": {
            "name": apk.name,
            "path": str(apk)
        },

        "package_name": package_name,

        "statistics": {

            "total_java_files":
                jadx_result["java_file_count"],

            "app_java_files":
                source_result["java_file_count"],

            "security_candidate_files":
                len(security_files)
        },

        # 앱 자체 코드 전체 목록
        "app_sources": java_files,

        # 보안 신호가 잡힌 파일
        # 단, Agent 분석 범위를 제한하는 용도가 아니라
        # 우선순위 힌트로 사용
        "security_candidates":
            security_files
    }

    result_path = (
        output_dir
        / "preprocess_result.json"
    )

    with result_path.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            preprocess_result,
            file,
            ensure_ascii=False,
            indent=2
        )

    print(
        "전처리 결과 저장 완료:",
        result_path
    )


      # =================================
    # STEP 5.5. 에뮬레이터에 APK 설치
    # =================================

    print()
    print("[STEP 5.5] 에뮬레이터에 APK 설치")

    ADB_PATH = r"C:\Users\pcybb\AppData\Local\Android\Sdk\platform-tools\adb.exe"

    install_result = subprocess.run(
        [ADB_PATH, "install", "-r", str(apk)],
        capture_output=True, text=True
    )

    print(install_result.stdout.strip())

    if "Success" not in install_result.stdout:
        print("경고: APK 설치 실패 또는 이미 설치됨. 계속 진행합니다.")

    # =================================
    # STEP 6. Mobile Red Teaming Agent
    # =================================

    print()
    print("[STEP 6] Mobile Red Teaming Agent 실행")

    try:

        agent_result = run_mobile_agent(
            output_dir=str(output_dir),
            adb_path=ADB_PATH,
            package_name=package_name
        )

    except Exception as error:

        print("Agent 실행 실패")
        print("오류:", error)

        return


    # =================================
    # STEP 7. Agent 결과 출력
    # =================================

    print()
    print("=== Agent 최종 분석 결과 ===")
    print()

    print(agent_result)


    # =================================
    # STEP 8. Agent 보고서 저장
    # =================================

    agent_report_path = (
        output_dir
        / "agent_report.txt"
    )

    agent_report_path.write_text(
        agent_result,
        encoding="utf-8"
    )

    print()
    print(
        "Agent 보고서 저장 완료:",
        agent_report_path
    )


    # =================================
    # 완료
    # =================================

    print()
    print("=== APK 분석 프로토타입 완료 ===")


if __name__ == "__main__":
    main()