"""Orchestrate minni wire for one or more platforms."""

from __future__ import annotations

import importlib.resources
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from minni.wire.from_repo import build_from_repo, self_check_manifest
from minni.wire.gc import run_gc
from minni.wire.install import (
    HashMismatchError,
    InstallError,
    install_payload,
)
from minni.wire.manifest import (
    PayloadManifest,
    package_version,
    verify_manifest_hashes,
)
from minni.wire.output import PlatformResult, WireOutput
from minni.wire.paths import is_safe_version_segment, plugin_base
from minni.wire.platform import (
    GEMINI_PROVISIONAL_REASON,
    GEMINI_SKIP_WARNING,
    PlatformSpec,
    expand_platforms,
    platform_spec,
)
from minni.wire.preflight import preflight_platform
from minni.wire.verify import run_verify
from minni.wire.wired import make_record, upsert_wire
from minni.wire.writers import (
    bootstrap_vault,
    load_json,
    mcp_json,
    native_afm_env,
    update_antigravity_config,
    update_agy_plugin_hooks,
    update_claude_config,
    update_kilo_config,
    update_toml_mcp_config,
    vault_for,
    write_json,
)


class WireError(Exception):
    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@contextmanager
def payload_tree(
    *,
    from_repo: Path | None,
    use_version: str | None,
) -> Iterator[tuple[Path, PayloadManifest, bool]]:
    """Yield (payload_root, manifest, is_ephemeral)."""
    if use_version:
        if not is_safe_version_segment(use_version):
            raise WireError(
                f"invalid --use-version {use_version!r}",
                exit_code=2,
            )
        root = plugin_base() / use_version
        manifest_path = root / "payload-manifest.json"
        if not root.is_dir():
            raise WireError(f"version dir not found: {root}", exit_code=2)
        if not manifest_path.is_file():
            raise WireError(
                f"missing payload-manifest.json in {root}",
                exit_code=2,
            )
        manifest = PayloadManifest.load(manifest_path)
        if not is_safe_version_segment(manifest.version):
            raise WireError(
                f"manifest version {manifest.version!r} is not a valid version segment",
                exit_code=2,
            )
        if manifest.version != use_version:
            raise WireError(
                f"manifest version {manifest.version!r} != --use-version {use_version!r}",
                exit_code=2,
            )
        yield root, manifest, False
        return

    if from_repo is not None:
        tmp, manifest = build_from_repo(from_repo)
        try:
            self_check_manifest(manifest)
            if not is_safe_version_segment(manifest.version):
                raise WireError(
                    f"payload version {manifest.version!r} is not a valid version segment",
                    exit_code=2,
                )
            yield tmp, manifest, True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return

    ref = importlib.resources.files("minni") / "plugin_payload"
    manifest_ref = ref / "payload-manifest.json"
    try:
        manifest_ref.open("r")
    except (FileNotFoundError, TypeError, OSError):
        raise WireError(
            "no bundled plugin payload in this install; use "
            "`--from-repo ~/Projects/Minni` (requires Node) or install a released wheel",
            exit_code=2,
        ) from None

    with importlib.resources.as_file(ref) as payload_root:
        manifest = PayloadManifest.load(payload_root / "payload-manifest.json")
        if not is_safe_version_segment(manifest.version):
            raise WireError(
                f"payload version {manifest.version!r} is not a valid version segment",
                exit_code=2,
            )
        pkg_ver = package_version()
        if manifest.version != pkg_ver:
            raise WireError(
                f"payload version {manifest.version!r} != installed package {pkg_ver!r}",
                exit_code=2,
            )
        yield payload_root, manifest, False


def _wire_platform(
    spec: PlatformSpec,
    install_root: Path,
    version: str,
    *,
    socket: Path,
    workspace: Path | None,
    repo_root: Path | None,
    explicit_workspace: bool,
    dry_run: bool,
    mcp_root: Path | None = None,
) -> tuple[Path | None, dict[str, object]]:
    server_path = install_root / "dist" / "server.js"
    agent = spec.agent
    vault = bootstrap_vault(agent) if not dry_run else vault_for(agent)
    afm_env = native_afm_env(repo_root)
    stamp_workspace = workspace or Path.home() / "Projects" / "Minni"
    extras: dict[str, object] = {}

    mcp_target = (mcp_root or install_root) / ".mcp.json"
    pre_doc: dict = {}
    pre_mcp_env: dict = {}
    if mcp_target.exists():
        try:
            pre_doc = load_json(mcp_target)
            pre_mcp_env = (
                pre_doc.get("mcpServers", {}).get("minni", {}).get("env", {}) or {}
            )
        except Exception:
            pre_doc = {}
            pre_mcp_env = {}

    if not dry_run:
        generated = mcp_json(
            server_path, agent, vault, socket, stamp_workspace,
            pre_existing_env=pre_mcp_env,
            explicit_workspace=explicit_workspace,
            afm_env=afm_env,
        )
        # Merge into the existing document: only the minni entry is ours;
        # unrelated MCP servers and top-level keys must survive a wire.
        merged = dict(pre_doc) if isinstance(pre_doc, dict) else {}
        servers = merged.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        servers = dict(servers)
        servers["minni"] = generated["mcpServers"]["minni"]
        merged["mcpServers"] = servers
        write_json(mcp_target, merged)

    config_path: Path | None = spec.config_path
    kind = spec.config_kind

    if kind == "gemini-provisional":
        return None, {"reason": GEMINI_PROVISIONAL_REASON}

    if kind == "mcp-json-only":
        config_path = mcp_target
        return config_path, extras

    if not dry_run:
        if kind == "toml" and config_path is not None:
            update_toml_mcp_config(
                config_path, server_path, agent, vault, socket, stamp_workspace,
                explicit_workspace=explicit_workspace, afm_env=afm_env,
            )
        elif kind == "claude-json":
            config_path = update_claude_config(
                server_path, agent, vault, socket, stamp_workspace, afm_env,
            )
        elif kind == "kilo-json":
            config_path = update_kilo_config(
                server_path, agent, vault, socket, stamp_workspace, afm_env,
            )
        elif kind == "antigravity":
            extras["antigravity"] = update_antigravity_config(
                install_root, agent, vault, socket, stamp_workspace, afm_env,
            )
            extras["agy_hooks"] = update_agy_plugin_hooks(install_root)
            views_written = extras["antigravity"].get("views_written", [])
            if views_written:
                config_path = Path(views_written[0])
            else:
                config_path = Path("~/.gemini/config/mcp_config.json").expanduser()

    return config_path, extras


def run_wire(args) -> int:
    out = WireOutput()
    dry_run = bool(args.dry_run)
    verify_payload = bool(args.verify_payload)
    stdin_is_tty = sys.stdin.isatty()
    prune_flag = None
    if args.prune:
        prune_flag = True
    elif args.no_prune:
        prune_flag = False

    try:
        platforms, warnings = expand_platforms(args.platform)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        out.status = "failed"
        out.results.append(PlatformResult("?", "failed", reason=str(exc)))
        print(json_preflight(out))
        return 2

    for plat, warn in warnings:
        print(f"[wire] warning: {warn}", file=sys.stderr)
        out.results.append(
            PlatformResult(plat, "skipped", reason=warn),
        )

    # Generic / mandatory args — exit 2 before filesystem changes
    plat_arg = canonical_platform(args.platform)
    if plat_arg == "generic":
        if not args.install_root:
            return _exit2("generic wire requires --install-root")
        if not args.agent:
            return _exit2(
                "generic wire requires --agent so it cannot inherit another agent's vault",
            )

    from minni.wire.preflight import check_node  # patched in tests
    ok, node_msg = check_node()
    if not ok:
        print(node_msg, file=sys.stderr)
        return _exit2(node_msg)

    use_version = args.use_version
    # Resolve here, not just inside build_from_repo: repo_root flows into
    # native_afm_env(), and a relative --from-repo (".") would stamp a relative
    # MINNI_AFM_NATIVE_HELPER into configs — broken the moment the host
    # launches the installed server from its own cwd.
    from_repo = Path(args.from_repo).expanduser().resolve() if args.from_repo else None
    repo_root = from_repo or Path.cwd()

    try:
        with payload_tree(from_repo=from_repo, use_version=use_version) as (
            payload_root, manifest, ephemeral,
        ):
            version = manifest.version
            out.payload_version = version

            if verify_payload and use_version:
                errors = verify_manifest_hashes(
                    manifest,
                    plugin_base() / use_version,
                )
                if errors:
                    raise WireError("; ".join(errors))

            install_root = plugin_base() / version
            out.install_root = str(install_root)

            if use_version is None:
                if verify_payload:
                    errors = verify_manifest_hashes(manifest, payload_root)
                    if errors:
                        raise WireError("; ".join(errors[:5]))

                if not dry_run:
                    try:
                        result = install_payload(
                            payload_root,
                            version,
                            manifest,
                            force_reinstall=args.force_reinstall,
                            dry_run=False,
                            update_current=True,
                        )
                        install_root = result.install_root
                        out.install_root = str(install_root)
                    except HashMismatchError as exc:
                        raise WireError(str(exc)) from exc
                    except InstallError as exc:
                        raise WireError(str(exc)) from exc
                else:
                    install_payload(
                        payload_root, version, manifest,
                        force_reinstall=args.force_reinstall,
                        dry_run=True,
                    )

            socket = Path(args.socket).expanduser()
            workspace = Path(args.workspace).expanduser() if args.workspace else None
            explicit_workspace = args.workspace is not None

            for platform in platforms:
                if any(r.platform == platform for r in out.results):
                    continue
                try:
                    spec = platform_spec(
                        platform,
                        install_root=args.install_root,
                        agent=args.agent,
                    )
                except ValueError as exc:
                    out.results.append(
                        PlatformResult(platform, "failed", reason=str(exc)),
                    )
                    continue

                if spec.config_kind == "gemini-provisional":
                    print(
                        f"[wire] {GEMINI_PROVISIONAL_REASON}",
                        file=sys.stderr,
                    )
                    out.results.append(
                        PlatformResult(
                            platform, "skipped", reason=GEMINI_PROVISIONAL_REASON,
                        ),
                    )
                    continue

                plat_errors = preflight_platform(platform)
                if plat_errors:
                    out.results.append(
                        PlatformResult(
                            platform, "failed", reason="; ".join(plat_errors),
                        ),
                    )
                    continue

                mcp_root = None
                if args.install_root:
                    mcp_root = Path(args.install_root).expanduser()
                try:
                    config_path, extras = _wire_platform(
                        spec, install_root, version,
                        socket=socket,
                        workspace=workspace,
                        repo_root=repo_root if from_repo else None,
                        explicit_workspace=explicit_workspace,
                        dry_run=dry_run,
                        mcp_root=mcp_root,
                    )
                except Exception as exc:
                    out.results.append(
                        PlatformResult(platform, "failed", reason=str(exc)),
                    )
                    continue

                server_path = str(install_root / "dist" / "server.js")
                verify = None
                if not dry_run:
                    vr = run_verify(
                        install_root, spec.hook_entry, config_path, spec.config_kind,
                    )
                    verify = {
                        "handshake": vr.handshake,
                        "hook_dry_run": vr.hook_dry_run,
                        "config_readback": vr.config_readback,
                    }
                    if not all(verify.values()):
                        out.results.append(PlatformResult(
                            platform, "failed",
                            config_path=str(config_path) if config_path else None,
                            server_path=server_path,
                            agent=spec.agent,
                            workspace=str(workspace) if workspace else None,
                            verify=verify,
                            reason="verification failed",
                            extra=extras,
                        ))
                        continue

                    record = make_record(
                        platform,
                        config_path or install_root / ".mcp.json",
                        install_root,
                        version,
                        str(workspace) if workspace else None,
                    )
                    upsert_wire(record, dry_run=False)

                out.results.append(PlatformResult(
                    platform, "wired" if not dry_run else "wired",
                    config_path=str(config_path) if config_path else None,
                    server_path=server_path,
                    agent=spec.agent,
                    workspace=str(workspace) if workspace else None,
                    verify=verify,
                    extra=extras,
                ))

            any_wired = any(r.status == "wired" for r in out.results)
            if not dry_run and any_wired:
                gc_result = run_gc(
                    prune=prune_flag,
                    stdin_is_tty=stdin_is_tty,
                    dry_run=False,
                )
                out.gc = {
                    "pruned": gc_result.pruned,
                    "retained_in_use": gc_result.retained_in_use,
                    "skipped_no_tty": gc_result.skipped_no_tty,
                }
            elif dry_run:
                gc_result = run_gc(
                    prune=prune_flag,
                    stdin_is_tty=stdin_is_tty,
                    dry_run=True,
                )
                out.gc = {
                    "pruned": gc_result.would_prune,
                    "retained_in_use": gc_result.retained_in_use,
                    "skipped_no_tty": gc_result.skipped_no_tty,
                }

    except WireError as exc:
        print(str(exc), file=sys.stderr)
        out.status = "failed"
        if not out.results:
            out.results.append(PlatformResult("?", "failed", reason=str(exc)))
        out.finalize_status(dry_run=dry_run)
        out.emit()
        return getattr(exc, "exit_code", 1)

    out.finalize_status(dry_run=dry_run)
    return out.emit()


def canonical_platform(platform: str) -> str:
    from minni.wire.platform import canonical_platform as _canon
    return _canon(platform)


def _exit2(message: str) -> int:
    print(message, file=sys.stderr)
    out = WireOutput(status="failed")
    out.results.append(PlatformResult("?", "failed", reason=message))
    import json
    print(json.dumps({
        "schema": 1,
        "status": "failed",
        "payload_version": None,
        "install_root": None,
        "results": [r.to_dict() for r in out.results],
        "gc": {},
    }, indent=2))
    return 2


def json_preflight(out: WireOutput) -> str:
    import json
    return json.dumps({
        "schema": out.schema,
        "status": out.status,
        "payload_version": out.payload_version,
        "install_root": out.install_root,
        "results": [r.to_dict() for r in out.results],
        "gc": out.gc,
    }, indent=2)