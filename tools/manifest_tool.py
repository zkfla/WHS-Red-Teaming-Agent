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
def get_exported_components(manifest_path: str):
    """
    AndroidManifest.xml에서 exported=true인 activity/service/receiver를 추출한다.
    """
    manifest = Path(manifest_path)

    if not manifest.exists():
        return {"success": False, "error": f"Manifest 파일이 없습니다: {manifest}"}

    try:
        tree = ET.parse(manifest)
        root = tree.getroot()
        ns = "{http://schemas.android.com/apk/res/android}"
        application = root.find("application")

        exported = {"activities": [], "services": [], "receivers": []}
        tag_map = {"activity": "activities", "service": "services", "receiver": "receivers"}

        for tag, key in tag_map.items():
            for node in application.findall(tag):
                name = node.attrib.get(f"{ns}name")
                exported_attr = node.attrib.get(f"{ns}exported")
                has_intent_filter = node.find("intent-filter") is not None

                is_exported = exported_attr == "true" or (exported_attr is None and has_intent_filter)

                if is_exported and name:
                    exported[key].append(name)

        return {"success": True, "exported": exported}

    except Exception as e:
        return {"success": False, "error": str(e)}