from pathlib import Path
import xml.etree.ElementTree as ET


def get_package_name(manifest_path: str):
    """
    AndroidManifest.xml에서 package 이름을 가져온다.
    """

    manifest = Path(manifest_path)

    if not manifest.exists():
        return {
            "success": False,
            "error": f"Manifest 파일이 없습니다: {manifest}"
        }

    try:
        tree = ET.parse(manifest)
        root = tree.getroot()

        package_name = root.attrib.get("package")

        if not package_name:
            return {
                "success": False,
                "error": "package 이름을 찾지 못했습니다."
            }

        return {
            "success": True,
            "package_name": package_name
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }