"""Interactive setup menu."""

from __future__ import annotations

import copy
import logging

import questionary
from questionary import Choice

from . import runner
from .config import (
    PLACEHOLDERS,
    Config,
    ConfigError,
    Rule,
    load,
    resolve_path,
    save,
    validate_url_template,
)
from .events import EVENT_TYPES
from .isapi import AuthError, NvrClient, NvrError

NOTIFY_HINT = (
    "Tip: on the NVR, enable 'Notify Surveillance Center' in the linkage/action\n"
    "settings of each camera's Motion (or Smart Event), or no events will be sent."
)

TARGET_CHOICES = {
    "any": ([], "Any (no filter)"),
    "human": (["human"], "Human only"),
    "vehicle": (["vehicle"], "Vehicle only"),
    "both": (["human", "vehicle"], "Human or vehicle"),
}


class Cancelled(Exception):
    pass


def ask(question):
    """Run a questionary prompt; Ctrl+C cancels the current action, not the program."""
    answer = question.ask()
    if answer is None:
        raise Cancelled
    return answer


def info(msg: str) -> None:
    questionary.print(msg, style="fg:ansicyan")


def ok(msg: str) -> None:
    questionary.print(msg, style="fg:ansigreen")


def error(msg: str) -> None:
    questionary.print(msg, style="fg:ansired")


class Menu:
    def __init__(self, path) -> None:
        self.path = resolve_path(path)
        try:
            self.config = load(self.path)
            info(f"Loaded {self.path}")
        except FileNotFoundError:
            self.config = Config()
            info(f"No config at {self.path} yet; let's create one.")
        except ConfigError as exc:
            error(f"{self.path} is invalid ({exc}); starting with an empty config.")
            self.config = Config()

    def save(self) -> None:
        save(self.config, self.path)
        ok(f"Saved to {self.path}")

    # ----------------------------------------------------------------- main menu

    def run(self) -> int:
        if not self.config.nvr.host:
            try:
                self.configure_nvr()
            except Cancelled:
                pass
        while True:
            nvr = self.config.nvr.host or "not configured"
            enabled = sum(r.enabled for r in self.config.rules)
            print()
            choice = questionary.select(
                f"NVR: {nvr} | rules: {enabled} enabled / {len(self.config.rules)}",
                choices=[
                    Choice("Start forwarding events", "run"),
                    Choice("Manage forwarding rules", "rules"),
                    Choice("Watch live events (no forwarding)", "watch"),
                    Choice("List cameras", "cameras"),
                    Choice("Send a test request", "test"),
                    Choice("Configure NVR connection", "nvr"),
                    Choice("Quit", "quit"),
                ],
            ).ask()
            if choice in (None, "quit"):
                return 0
            try:
                getattr(self, f"action_{choice}")()
            except Cancelled:
                info("Cancelled.")

    def action_run(self) -> None:
        if not self._require_nvr():
            return
        if not any(r.enabled for r in self.config.rules):
            if not ask(questionary.confirm("No enabled rules. Start anyway?", default=False)):
                return
        info(f"Forwarding events. Press Ctrl+C to return to the menu.\n{NOTIFY_HINT}")
        try:
            runner.run_forwarder(self.config)
        except AuthError as exc:
            error(str(exc))
        except KeyboardInterrupt:
            info("\nStopped.")

    def action_watch(self) -> None:
        if not self._require_nvr():
            return
        show_all = ask(questionary.confirm("Include idle heartbeat messages?", default=False))
        raw = ask(questionary.confirm("Show raw event data?", default=False))
        info("Watching events. Press Ctrl+C to return to the menu.")
        try:
            runner.watch(self.config, show_all=show_all, raw=raw)
        except AuthError as exc:
            error(str(exc))
        except KeyboardInterrupt:
            info("\nStopped.")

    def action_cameras(self) -> None:
        if not self._require_nvr():
            return
        if self._fetch_cameras():
            for cid, name in self.config.cameras.items():
                print(f"  {cid:>3}  {name}")
            self.save()

    def action_test(self) -> None:
        rule = self._pick_rule("Which rule should be tested?")
        if rule is not None:
            self._send_test(rule)

    def action_nvr(self) -> None:
        self.configure_nvr()

    def action_rules(self) -> None:
        while True:
            choices = [
                Choice(f"{'✓' if r.enabled else '✗'} {r.name}  —  {self._rule_summary(r)}", r)
                for r in self.config.rules
            ]
            choices += [Choice("+ Add a new rule", "add"), Choice("← Back", "back")]
            picked = ask(questionary.select("Forwarding rules:", choices=choices))
            if picked == "back":
                return
            if picked == "add":
                new = self.edit_rule(None)
                if new is not None:
                    self.config.rules.append(new)
                    self.save()
                    self._offer_test(new)
                continue
            self._rule_actions(picked)

    # --------------------------------------------------------------- NVR setup

    def configure_nvr(self) -> None:
        nvr = copy.copy(self.config.nvr)
        info("NVR connection (Hikvision / Annke / other ISAPI-compatible recorders)")
        nvr.host = ask(
            questionary.text(
                "NVR IP address or hostname:",
                default=nvr.host,
                validate=lambda s: bool(s.strip()) or "Required",
            )
        ).strip()
        nvr.https = ask(questionary.confirm("Use HTTPS?", default=nvr.https))
        default_port = nvr.port if nvr.port not in (80, 443) else (443 if nvr.https else 80)
        nvr.port = int(
            ask(
                questionary.text(
                    "Port:",
                    default=str(default_port),
                    validate=lambda s: (s.isdigit() and 0 < int(s) < 65536) or "Invalid port",
                )
            )
        )
        if nvr.https:
            nvr.verify_tls = ask(
                questionary.confirm(
                    "Verify the TLS certificate? (NVRs usually use self-signed ones)",
                    default=nvr.verify_tls,
                )
            )
        nvr.username = ask(questionary.text("Username:", default=nvr.username)).strip()
        hint = " (leave blank to keep current)" if nvr.password else ""
        password = ask(questionary.password(f"Password{hint}:"))
        if password:
            nvr.password = password
        nvr.auth = ask(
            questionary.select(
                "Authentication:",
                choices=[
                    Choice("Digest (standard for Hikvision/Annke)", "digest"),
                    Choice("Basic (only if Digest fails)", "basic"),
                ],
                default=nvr.auth if nvr.auth in ("digest", "basic") else "digest",
            )
        )

        info(f"Testing connection to {nvr.base_url} ...")
        old_nvr, self.config.nvr = self.config.nvr, nvr
        try:
            device = NvrClient(nvr).device_info()
            ok(
                "Connected: "
                + " / ".join(
                    v
                    for v in (
                        device.get("deviceName"),
                        device.get("model"),
                        device.get("firmwareVersion"),
                    )
                    if v
                )
            )
            if self._fetch_cameras():
                ok(f"Found {len(self.config.cameras)} camera(s).")
        except NvrError as exc:
            error(f"Connection test failed: {exc}")
            if not ask(questionary.confirm("Save these settings anyway?", default=False)):
                self.config.nvr = old_nvr
                return
        self.save()
        info(NOTIFY_HINT)

    def _fetch_cameras(self) -> bool:
        try:
            cameras = NvrClient(self.config.nvr).list_channels()
        except NvrError as exc:
            error(f"Could not list cameras: {exc}")
            return False
        if not cameras:
            error("The NVR did not report any channels.")
            return False
        self.config.cameras = cameras
        return True

    def _require_nvr(self) -> bool:
        if self.config.nvr.host:
            return True
        error("Configure the NVR connection first.")
        return False

    # ------------------------------------------------------------------- rules

    def _rule_summary(self, rule: Rule) -> str:
        cams = ", ".join(self.config.camera_name(c) for c in rule.channels) or "all cameras"
        events = ", ".join(EVENT_TYPES.get(e, e) for e in rule.event_types)
        target = f" [{'/'.join(rule.targets)}]" if rule.targets else ""
        return f"{events}{target} on {cams}"

    def _pick_rule(self, prompt: str) -> Rule | None:
        if not self.config.rules:
            error("No rules yet. Add one under 'Manage forwarding rules'.")
            return None
        return ask(
            questionary.select(prompt, choices=[Choice(r.name, r) for r in self.config.rules])
        )

    def _rule_actions(self, rule: Rule) -> None:
        print(f"  URL:      {rule.url}")
        print(f"  Cooldown: {self.config.cooldown_for(rule):g}s")
        action = ask(
            questionary.select(
                f"Rule '{rule.name}':",
                choices=[
                    Choice("Edit", "edit"),
                    Choice("Disable" if rule.enabled else "Enable", "toggle"),
                    Choice("Send a test request", "test"),
                    Choice("Delete", "delete"),
                    Choice("← Back", "back"),
                ],
            )
        )
        if action == "edit":
            updated = self.edit_rule(rule)
            if updated is not None:
                self.config.rules[self.config.rules.index(rule)] = updated
                self.save()
        elif action == "toggle":
            rule.enabled = not rule.enabled
            self.save()
        elif action == "test":
            self._send_test(rule)
        elif action == "delete":
            if ask(questionary.confirm(f"Delete rule '{rule.name}'?", default=False)):
                self.config.rules.remove(rule)
                self.save()

    def edit_rule(self, existing: Rule | None) -> Rule | None:
        rule = copy.deepcopy(existing) if existing else Rule(name="", url="")
        taken = {r.name for r in self.config.rules if r is not existing}

        rule.name = ask(
            questionary.text(
                "Rule name:",
                default=rule.name,
                validate=lambda s: (
                    "Required" if not s.strip() else "Name already used" if s.strip() in taken else True
                ),
            )
        ).strip()

        rule.channels = self._ask_channels(rule.channels)

        choices = [
            Choice(f"{label} ({code})", code, checked=code in rule.event_types)
            for code, label in EVENT_TYPES.items()
        ]
        choices += [Choice(e, e, checked=True) for e in rule.event_types if e not in EVENT_TYPES]
        rule.event_types = ask(
            questionary.checkbox(
                "Which events should trigger the request? (space to select)",
                choices=choices,
                validate=lambda sel: bool(sel) or "Pick at least one",
            )
        )

        current = next(
            (k for k, (v, _) in TARGET_CHOICES.items() if sorted(v) == sorted(rule.targets)),
            "any",
        )
        target_key = ask(
            questionary.select(
                "Filter by detected target? (needs AcuSense/smart cameras that report it)",
                choices=[Choice(label, key) for key, (_, label) in TARGET_CHOICES.items()],
                default=current,
            )
        )
        rule.targets = list(TARGET_CHOICES[target_key][0])

        info("Placeholders you can use in the URL:")
        for name, desc in PLACEHOLDERS.items():
            print(f"  {{{name}}}".ljust(18) + desc)

        def check_url(s: str):
            try:
                validate_url_template(s.strip())
            except ConfigError as exc:
                return str(exc)
            return True

        rule.url = ask(
            questionary.text(
                "URL to GET:",
                default=rule.url or "http://192.168.1.50:8080/motion?camera={camera_name}&event={event_type}",
                validate=check_url,
            )
        ).strip()

        default_cd = self.config.cooldown_for(rule)
        cooldown = ask(
            questionary.text(
                "Cooldown in seconds (minimum time between requests per camera):",
                default=f"{default_cd:g}",
                validate=_is_non_negative_number,
            )
        )
        cooldown_value = float(cooldown)
        rule.cooldown_seconds = (
            None if cooldown_value == self.config.defaults.cooldown_seconds else cooldown_value
        )
        return rule

    def _ask_channels(self, current: list[int]) -> list[int]:
        if not self.config.cameras and self.config.nvr.host:
            info("Fetching cameras from the NVR ...")
            self._fetch_cameras()

        if not self.config.cameras:
            raw = ask(
                questionary.text(
                    "Channel numbers, comma-separated (blank = all cameras):",
                    default=", ".join(map(str, current)),
                    validate=lambda s: all(p.strip().isdigit() for p in s.split(",") if p.strip())
                    or "Use numbers like 1, 3",
                )
            )
            return [int(p) for p in raw.split(",") if p.strip()]

        scope = ask(
            questionary.select(
                "Which cameras?",
                choices=[Choice("All cameras", "all"), Choice("Choose specific cameras", "some")],
                default="some" if current else "all",
            )
        )
        if scope == "all":
            return []
        return ask(
            questionary.checkbox(
                "Select cameras (space to select, enter to confirm):",
                choices=[
                    Choice(f"{cid:>3}  {name}", cid, checked=cid in current)
                    for cid, name in self.config.cameras.items()
                ],
                validate=lambda sel: bool(sel) or "Pick at least one",
            )
        )

    def _offer_test(self, rule: Rule) -> None:
        if ask(questionary.confirm("Send a test request now?", default=True)):
            self._send_test(rule)

    def _send_test(self, rule: Rule) -> None:
        resp = runner.send_test(self.config, rule)
        if resp is None:
            error("Request failed (see the log line above).")
        elif resp.ok:
            ok(f"Success: HTTP {resp.status_code}")
        else:
            error(f"Server answered HTTP {resp.status_code}")


def _is_non_negative_number(s: str):
    try:
        return float(s) >= 0 or "Must be 0 or more"
    except ValueError:
        return "Enter a number"


def run_menu(path) -> int:
    # Keep log lines (connection status, requests sent) visible but compact.
    logging.getLogger().setLevel(min(logging.getLogger().level, logging.INFO))
    try:
        return Menu(path).run()
    except KeyboardInterrupt:
        return 130
