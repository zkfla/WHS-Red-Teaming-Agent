"""Built-in observation bundles: one contribution per tool mapper (M05/M18).

Each bundle names the tools one mapper serves, the mapper's import
reference (resolved only at assembly time, never serialized as a live
function), its version, and the extra graph-attribute keys whose change
counts as meaningful for reactivation fingerprints. An empty key list
means no extra fingerprint contribution -- same as today -- never a
block on running the tool.
"""

from __future__ import annotations

from redteam.orchestration.observations.mapper import MapperContribution


def _bundle(
    tool_id: str,
    ref: str,
    *,
    keys: tuple[str, ...] = (),
    version: str = "1",
) -> MapperContribution:
    return MapperContribution(
        tool_ids=(tool_id,),
        mapper_ref=ref,
        mapper_version=version,
        meaningful_change_keys=keys,
    )


def builtin_observation_packages() -> tuple[MapperContribution, ...]:
    """Every always-on mapper bundle (the MobSF one is conditional)."""
    base = "redteam.orchestration.observations"
    return (
        _bundle("analysis.decode_apk", f"{base}.mobile_analysis:map_apktool_decode"),
        _bundle("analysis.get_manifest", f"{base}.mobile_analysis:map_apktool_manifest"),
        _bundle(
            "mobile.mobile_get_device_logs",
            f"{base}.mobile_runtime:map_mobile_device_logs",
            keys=("mobile_runtime_crash_observed",),
        ),
        _bundle(
            "mobile.mobile_list_crashes",
            f"{base}.mobile_runtime:map_mobile_crash_list",
            keys=("mobile_crash_observed",),
        ),
        _bundle(
            "mobile.mobile_get_crash",
            f"{base}.mobile_runtime:map_mobile_crash_detail",
            keys=("mobile_runtime_crash_observed",),
        ),
        _bundle(
            "recon.dns_resolve",
            f"{base}.recon_network:map_recon_dns_resolve",
            keys=("dns_resolved",),
        ),
        _bundle(
            "recon.http_probe",
            f"{base}.recon_http:map_recon_http_probe",
            keys=("security_headers_present",),
        ),
        _bundle(
            "recon.tls_inspect",
            f"{base}.recon_network:map_recon_tls_inspect",
            keys=("tls_verified",),
        ),
        _bundle(
            "recon.html_inventory",
            f"{base}.recon_inventory:map_recon_html_inventory",
            keys=("path_allowlisted",),
        ),
        _bundle(
            "recon.openapi_inventory",
            f"{base}.recon_inventory:map_recon_openapi_inventory",
            keys=("path_allowlisted",),
        ),
        _bundle(
            "recon.oidc_inventory",
            f"{base}.recon_network:map_recon_oidc_inventory",
            keys=("oidc_metadata_observed",),
        ),
        _bundle(
            "recon.port_scan",
            f"{base}.recon_network:map_recon_port_scan",
            keys=("port_scan_complete",),
        ),
        _bundle(
            "web.http_request",
            f"{base}.web_http:map_web_http_request",
            keys=("security_headers_present",),
            version="2",
        ),
        _bundle(
            "web.http_compare_sessions",
            f"{base}.web_http:map_web_compare_sessions",
            keys=("session_comparison_observed",),
        ),
        _bundle("web.http_compare_anonymous", f"{base}.web_http:map_web_compare_anonymous"),
        _bundle(
            "web.http_security_posture",
            f"{base}.web_http:map_web_http_security_posture",
            keys=("security_posture_observed",),
        ),
        _bundle(
            "web.browser_navigate",
            f"{base}.web_browser:map_web_browser_navigate",
            keys=("browser_navigated",),
        ),
        _bundle("web.session_list", f"{base}.web_http:map_web_session_list"),
        _bundle(
            "web.proxy_list_flows",
            f"{base}.web_proxy:map_web_proxy_list_flows",
            keys=("proxy_observed",),
        ),
        _bundle(
            "web.proxy_get_flow",
            f"{base}.web_proxy:map_web_proxy_get_flow",
            keys=("proxy_observed",),
        ),
        _bundle("web.proxy_replay_flow", f"{base}.web_proxy:map_web_proxy_replay_flow"),
        _bundle("web.proxy_mutate_json", f"{base}.web_proxy:map_web_proxy_mutate_json"),
        _bundle(
            "web.proxy_compare_sessions",
            f"{base}.web_proxy:map_web_proxy_compare_sessions",
            keys=("session_comparison_observed",),
        ),
        _bundle(
            "web.zap_capture_snapshot",
            f"{base}.web_proxy:map_web_zap_capture_snapshot",
            keys=("proxy_observed",),
        ),
        _bundle(
            "web.nuclei_response_check",
            f"{base}.web_proxy:map_web_nuclei_response_check",
            keys=("passive_check_observed",),
        ),
        _bundle(
            "web.web_search",
            f"{base}.public_lookup:map_public_search_results",
        ),
        _bundle(
            "web.fetch_page_text",
            f"{base}.public_lookup:map_public_page_read",
        ),
    )


def mobsf_observation_package() -> MapperContribution:
    """Conditional bundle: registered only when the MobSF tool exists."""
    return _bundle(
        "analysis.mobsf_report_summary",
        "redteam.orchestration.observations.mobsf:map_mobsf_report",
        keys=("mobsf_static_summary", "mobsf_scan_ref"),
    )


def builtin_extension_declaration():
    """All built-in bundles as one declaration for the composition root."""
    from redteam.core.extensions import ExtensionDeclaration
    from redteam.orchestration.observations.mapper import mapper_contribution

    return ExtensionDeclaration(
        extension_id="builtin.observations",
        version="1",
        contributions=tuple(
            mapper_contribution(package) for package in builtin_observation_packages()
        ),
    )
