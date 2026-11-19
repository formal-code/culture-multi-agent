"""Check declared material availability without opening or interpreting media."""
from pathlib import Path


EXTENSIONS = {
    "image": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"},
    "video": {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
}


def inspect_materials(packet, input_path: Path) -> dict:
    base = input_path.resolve().parent
    known = {item.material_id: item for item in packet.materials}
    referenced = {item.material_id: [] for item in packet.materials}
    issues = []
    checks = []

    for script in packet.scripts:
        for shot in script.shots:
            for material_id in shot.material_ids:
                location = {"script_id": script.script_id, "shot_id": shot.shot_id}
                if material_id not in known:
                    issues.append({**location, "material_id": material_id, "issue": "unknown_material_reference"})
                else:
                    referenced[material_id].append(location)

    for material in packet.materials:
        path_status = "not_provided"
        resolved_path = None
        extension_status = "not_checked"
        if material.file_path:
            candidate = Path(material.file_path).expanduser()
            if not candidate.is_absolute():
                candidate = base / candidate
            try:
                resolved_path = str(candidate.resolve(strict=False))
                if candidate.is_file():
                    path_status = "available"
                    extension_status = ("matches_declared_type" if candidate.suffix.lower() in EXTENSIONS[material.type]
                                        else "extension_type_mismatch")
                elif candidate.exists():
                    path_status = "not_a_file"
                else:
                    path_status = "missing"
            except OSError:
                path_status = "path_check_failed"
        elif material.url:
            path_status = "remote_not_downloaded"

        warnings = []
        if path_status in {"not_provided", "missing", "not_a_file", "path_check_failed"}:
            warnings.append("素材文件当前不可用或未提供")
        if extension_status == "extension_type_mismatch":
            warnings.append("文件扩展名与声明素材类型不一致，需人工确认")
        if material.usage_status != "approved":
            warnings.append("当前使用状态不是已批准")
        if not material.license_note.strip():
            warnings.append("未提供授权说明")
        if not referenced[material.material_id]:
            warnings.append("未关联到任何镜头")

        checks.append({
            "material_id": material.material_id,
            "type": material.type,
            "description": material.description,
            "declared_usage_status": material.usage_status,
            "file_status": path_status,
            "extension_status": extension_status,
            "resolved_path": resolved_path,
            "url": material.url,
            "license_note": material.license_note,
            "used_by": referenced[material.material_id],
            "warnings": warnings,
            "inspection_limit": "仅检查路径、文件类型声明、使用状态和授权备注；未打开媒体，也未核验版权或拍摄许可。",
        })

    return {"items": checks, "reference_issues": issues,
            "summary": {"declared": len(checks),
                        "available_local_files": sum(item["file_status"] == "available" for item in checks),
                        "remote_not_downloaded": sum(item["file_status"] == "remote_not_downloaded" for item in checks),
                        "unavailable_or_missing": sum(item["file_status"] in {"not_provided", "missing", "not_a_file", "path_check_failed"} for item in checks),
                        "with_warnings": sum(bool(item["warnings"]) for item in checks)}}
