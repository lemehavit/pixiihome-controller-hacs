"""Swedish explanations for controller reason codes (shown as ``reason_text``)."""

from __future__ import annotations

REASON_TEXT: dict[str, str] = {
    "automation_policy_not_authorized": "Styrpolicyn tillåter inte funktionen",
    "production_control_disabled": "Styrbehörigheten är avstängd",
    "price_automation_policy_not_authorized": "Styrpolicyn tillåter inte prisoptimering",
    "price_entity_required": "Ingen prissensor är vald",
    "price_entity_unavailable": "Prissensorn saknar värde",
    "price_entity_stale": "Priserna är för gamla",
    "price_series_empty": "Prissensorn har inga priser",
    "future_price_horizon_empty": "Det finns inga framtida priser att planera med",
    "future_price_horizon_incomplete": "Det saknas priser för vissa intervall",
    "price_usable_capacity_required": "Ange användbar batterikapacitet för prisplanering",
    "price_soc_limits_invalid": "Lägsta SOC måste vara lägre än högsta SOC",
    "price_usable_soc_unavailable": "Batteriets SOC eller effektgränser saknas",
    "plant_telemetry_not_safe": "Batteriets mätvärden är inte färska eller säkra",
    "plant_or_meter_telemetry_not_safe": "Batteriets eller elmätarens mätvärden är inte färska",
    "non_idle_service_active": "En annan tjänst eller ett schema körs just nu",
    "battery_calibration_running": "Batterikalibrering pågår; automatiken väntar",
    "running_service_unavailable": "Okänt vilken tjänst batteriet kör",
    "price_schedule_writes_disabled": "Förhandsvisning: skapande av scheman är avstängt",
    "price_scheduler_inventory_incomplete": "Schemalistan från Gateway är ofullständig",
    "price_scheduler_inventory_unavailable": "Schemalistan från Gateway saknas",
    "price_scheduler_mutation_unavailable": "Gateway-anslutning för scheman saknas",
    "price_schedule_identity_ambiguous": "Ett schema går inte att identifiera säkert",
    "price_recurring_schedule_scope_unknown": "Ett återkommande schema finns; planering stoppad",
    "price_external_schedule_energy_unknown": "Ett externt schema har okänd effekt på batteriet",
    "price_external_target_recovery_required": "Ändring av aggregatorschema behöver kontrolleras",
    "price_mutation_context_changed": "Inställningar eller priser ändrades; planerar om",
    "price_action_lead_time_elapsed": "Planerat intervall hann börja; planerar om",
    "price_action_exceeds_policy_duration": "Planerat intervall är längre än policyn tillåter",
    "price_stop_quarantine_active": "Ett stoppat schema väntar på bekräftelse",
    "price_controller_failed": "Oväntat fel i prisoptimeringen",
    "energy_forecast_unavailable": "Sol- eller förbrukningsprognos saknas",
    "solar_conflicts_with_adaptive_peak_shaving": "Adaptiv effekttoppskapning är påslagen",
    "solar_telemetry_incomplete": "Mätvärden för solladdning saknas",
    "solar_controller_failed": "Oväntat fel i solladdningen",
    "maximum_usable_soc_reached": "Högsta SOC för solladdning är nådd",
    "smart_solar_deferring": "Smart solladdning väntar: säljer nu, laddar senare",
    "smart_solar_export_now_charge_later": "Smart solladdning väntar: säljer nu, laddar senare",
    "solar_lease_not_renewed": "Solladdningens lease förnyades inte",
    "solar_lease_identity_missing": "Solladdningens schema-id saknas",
    "solar_lease_ownership_lost": "Solladdningens schema ägs inte längre av integrationen",
    "solar_lease_command_failed": "Kommandot för solladdning misslyckades",
    "solar_lease_verification_failed": "Solladdningen kunde inte bekräftas",
    "finite_lease_allowed_to_expire": "Avstängd; pågående lease får löpa ut",
    "foreign_adaptive_peak_shaving_active": "En annan Adaptive Peak Shaving körs redan",
    "aps_conflicts_with_automatic_solar_charging": "Automatisk solladdning är påslagen",
    "aps_telemetry_incomplete": "Mätvärden för effekttoppskapning saknas",
    "aps_controller_failed": "Oväntat fel i effekttoppskapningen",
    "aps_lease_identity_missing": "Effekttoppskapningens schema-id saknas",
    "aps_lease_ownership_lost": "Effekttoppskapningens schema ägs inte längre av integrationen",
    "aps_lease_command_failed": "Kommandot för effekttoppskapning misslyckades",
    "aps_lease_verification_failed": "Effekttoppskapningen kunde inte bekräftas",
    "solar_surplus_below_stop_threshold": "Solöverskottet är för litet",
    "solar_start_debounce": "Väntar på att solöverskottet ska hålla i sig",
    "load_measurement_incomplete": "Mätvärden för husets förbrukning saknas",
    "solar_forecast_unavailable": "Solprognosen saknas eller är för gammal",
    "history_unavailable": "Historik kunde inte läsas; lär sig från nu",
    "history_entities_missing": "Pixii-sensorerna för historik hittades inte",
}


def reason_text(reason: str | None) -> str:
    """Swedish text for a reason code; unknown codes are shown as-is."""
    if not reason:
        return ""
    if reason.startswith(("price_create_", "price_delete_")):
        return f"Ett schema kunde inte skapas eller tas bort ({reason})"
    return REASON_TEXT.get(reason, reason)


__all__ = ["REASON_TEXT", "reason_text"]
