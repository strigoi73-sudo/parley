"""Barriered Parley protocol bootstrap orchestration.

This module owns the A/B provisioning and activation sequence. Browser,
ChatGPT-DOM, attachment, and send/wait primitives are supplied explicitly by
the workflow layer so bootstrap orchestration remains independent of those
implementation details.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProtocolBootstrapOperations:
    validate_tab: object
    protocol_active: object
    adapter_for_tab: object
    snapshot_state: object
    attach_file: object
    wait_attachment_stable: object
    send_ack: object
    send_activation: object


def _emit_progress(progress, label, stage, status, **extra):
    if not callable(progress):
        return
    event = {"label": label, "stage": stage, "status": status}
    event.update(extra)
    progress(event)


def initialize_parley_pair(
    tab_a,
    tab_b,
    *,
    protocols,
    protocol_dir,
    attachment_stabilize_seconds,
    operations,
    wait_timeout_ms,
    should_stop=None,
    progress=None,
):
    """Provision and activate A/B protocols with explicit serial barriers."""
    if tab_a == tab_b:
        return {
            "ok": False,
            "error": "parley_same_tab",
            "stage": "preflight",
            "response_complete": False,
        }

    if not isinstance(operations, ProtocolBootstrapOperations):
        raise TypeError("operations must be ProtocolBootstrapOperations")

    participants = {}
    ordered = (
        ("A", tab_a, protocols["A"]),
        ("B", tab_b, protocols["B"]),
    )

    for label, tab_id, spec in ordered:
        validation = operations.validate_tab(tab_id)
        if not validation.get("ok"):
            return {
                "ok": False,
                "error": validation.get(
                    "error",
                    "parley_tab_preflight_failed",
                ),
                "stage": "preflight",
                "participant": label,
                "response_complete": False,
                "detail": validation,
                "participants": participants,
            }

        active = operations.protocol_active(tab_id, spec)
        participants[label] = {
            "tab_id": tab_id,
            "already_active": active,
        }
        _emit_progress(
            progress,
            label,
            "preflight",
            "already_active" if active else "pending",
        )

    # Phase 1: A file + ACK, then B file + ACK.
    for label, tab_id, spec in ordered:
        if participants[label]["already_active"]:
            _emit_progress(
                progress,
                label,
                "protocol_ready",
                "complete",
                reused=True,
            )
            continue

        protocol_path = protocol_dir / spec["filename"]
        adapter = operations.adapter_for_tab(tab_id)
        pre_attachment_state = operations.snapshot_state(
            tab_id,
            adapter=adapter,
            should_stop=should_stop,
        )
        if not pre_attachment_state.get("ok"):
            return {
                "ok": False,
                "error": pre_attachment_state.get(
                    "error",
                    "parley_protocol_snapshot_failed",
                ),
                "stage": "protocol_snapshot",
                "participant": label,
                "response_complete": False,
                "detail": pre_attachment_state,
                "participants": participants,
            }

        _emit_progress(
            progress,
            label,
            "provision",
            "starting",
            filename=spec["filename"],
        )

        attachment = operations.attach_file(
            tab_id,
            protocol_path,
            should_stop=should_stop,
        )
        participants[label]["attachment"] = attachment
        if not attachment.get("ok"):
            return {
                "ok": False,
                "error": attachment.get(
                    "error",
                    "parley_protocol_attachment_failed",
                ),
                "stage": "protocol_attachment",
                "participant": label,
                "response_complete": False,
                "detail": attachment,
                "participants": participants,
            }

        _emit_progress(
            progress,
            label,
            "attachment_stabilizing",
            "waiting",
            seconds=attachment_stabilize_seconds,
        )
        if not operations.wait_attachment_stable(
            should_stop=should_stop,
        ):
            return {
                "ok": False,
                "error": "chatgpt_wait_stopped",
                "stage": "attachment_stabilizing",
                "participant": label,
                "response_complete": False,
                "participants": participants,
            }

        ack_prompt = (
            "Read the attached Parley protocol file. Do not initialize or "
            "apply the Parley test protocol yet. Reply exactly with the "
            "following text and nothing else:\n\n"
            + spec["ack"]
        )
        _emit_progress(
            progress,
            label,
            "protocol_ack",
            "waiting",
        )
        ack_result = operations.send_ack(
            tab_id,
            ack_prompt,
            wait_timeout_ms=wait_timeout_ms,
            adapter=adapter,
            should_stop=should_stop,
            pre_state_override=pre_attachment_state,
            require_user_text_match=False,
            submission_timeout_ms=60000,
            expected_reply_prefix=spec["ack"],
            expected_reply_suffix=spec["ack"],
            retry_unsent_submission=True,
            required_attachment_filename=spec["filename"],
        )
        participants[label]["provision"] = ack_result
        if (
            not isinstance(ack_result, dict)
            or ack_result.get("error")
            or not ack_result.get("response_complete")
        ):
            return {
                "ok": False,
                "error": (
                    ack_result.get("error")
                    if isinstance(ack_result, dict)
                    else "parley_protocol_ack_failed"
                ) or "parley_protocol_ack_failed",
                "stage": "protocol_ack",
                "participant": label,
                "response_complete": False,
                "detail": ack_result,
                "participants": participants,
            }

        observed = (ack_result.get("response_text") or "").strip()
        if observed != spec["ack"]:
            return {
                "ok": False,
                "error": "parley_protocol_ack_mismatch",
                "stage": "protocol_ack",
                "participant": label,
                "response_complete": False,
                "expected": spec["ack"],
                "observed": observed,
                "participants": participants,
            }

        _emit_progress(
            progress,
            label,
            "protocol_ready",
            "complete",
        )

    # Global barrier: both protocols are verified before activation begins.
    for label, tab_id, spec in ordered:
        if participants[label]["already_active"]:
            _emit_progress(
                progress,
                label,
                "ready",
                "complete",
                reused=True,
            )
            continue

        _emit_progress(
            progress,
            label,
            "activation",
            "starting",
        )
        activation = operations.send_activation(
            tab_id,
            spec["activation"],
            wait_timeout_ms=wait_timeout_ms,
            expected_reply_prefix=spec["reply_prefix"],
            expected_reply_suffix=spec["reply_suffix"],
            should_stop=should_stop,
        )
        participants[label]["activation"] = activation
        if (
            not isinstance(activation, dict)
            or activation.get("error")
            or not activation.get("response_complete")
        ):
            return {
                "ok": False,
                "error": (
                    activation.get("error")
                    if isinstance(activation, dict)
                    else "parley_protocol_activation_failed"
                ) or "parley_protocol_activation_failed",
                "stage": "activation",
                "participant": label,
                "response_complete": False,
                "detail": activation,
                "participants": participants,
            }

        _emit_progress(
            progress,
            label,
            "ready",
            "complete",
            response_text=activation.get("response_text"),
        )

    return {
        "ok": True,
        "response_complete": True,
        "stage": "ready",
        "participants": participants,
    }
