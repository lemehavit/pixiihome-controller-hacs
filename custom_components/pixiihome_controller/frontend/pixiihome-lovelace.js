const PIXII_SERVICES = {
  targetsoc: "Batterimål (Target SoC)", demandresponse: "Laddning / urladdning med vald effekt",
  peakshaving: "Effekttoppskapning", adaptivepeakshaving: "Adaptiv effekttoppskapning",
};
function pixiiServiceValues(draft) {
  if (draft.service === "targetsoc") return {socref: Number(draft.soc)};
  const kw = key => {
    if (draft[key] === "" || !Number.isFinite(Number(draft[key]))) throw new Error("Ange ett giltigt värde i alla effektfält.");
    return Math.round(Number(draft[key]) * 1000);
  };
  if (draft.service === "demandresponse") {
    const power = kw("power");
    if (power <= 0) throw new Error("Effekten måste vara större än noll.");
    return {pacref: draft.direction === "discharge" ? -power : power};
  }
  const values = {exportlim: kw("exportLimit"), importlim: kw("importLimit"), charge_max: kw("chargeMax"),
    discharge_max: kw("dischargeMax"), keep_active: draft.keepActive};
  if (values.exportlim > values.importlim) throw new Error("Laddningsgränsen vid elmätaren får inte överstiga urladdningsgränsen.");
  if (values.charge_max < 0 || values.discharge_max < 0) throw new Error("Maxeffekterna får inte vara negativa.");
  if (draft.service === "adaptivepeakshaving") values.max_lim_change = kw("maxChange");
  return values;
}
function pixiiServiceSummary(service, values) {
  if (service === "targetsoc") return `Batterimål ${values.socref} %`;
  if (service === "demandresponse") return `${values.pacref < 0 ? "Urladdning" : "Laddning"} ${Math.abs(values.pacref) / 1000} kW`;
  return `Laddningsgräns ${values.exportlim / 1000} kW · urladdningsgräns ${values.importlim / 1000} kW · max laddning ${values.charge_max / 1000} kW · max urladdning ${values.discharge_max / 1000} kW · behåll aktiv: ${values.keep_active ? "ja" : "nej"}${service === "adaptivepeakshaving" ? ` · max gränsändring ${values.max_lim_change / 1000} kW` : ""}`;
}
const PIXII_ZONE = "Europe/Stockholm";
const pixiiClock = new Intl.DateTimeFormat("sv-SE", {
  timeZone: PIXII_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
function pixiiLocalTime(epoch) {
  const p = Object.fromEntries(pixiiClock.formatToParts(new Date(epoch * 1000)).map(v => [v.type, v.value]));
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}`;
}
function pixiiTimeCandidates(value) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(value)) return [];
  const full = value.length === 16 ? `${value}:00` : value;
  const naive = Date.parse(`${full}Z`) / 1000;
  return [naive - 7200, naive - 3600].filter(epoch => Number.isFinite(epoch) && pixiiLocalTime(epoch) === full);
}
function pixiiEpoch(value, fold = "", original = null) {
  const candidates = pixiiTimeCandidates(value);
  if (!candidates.length) throw new Error("Tiden finns inte i Europe/Stockholm. Kontrollera datum och övergången till sommartid.");
  if (!fold && candidates.includes(original)) return original;
  if (candidates.length > 1 && !fold) throw new Error("Tiden inträffar två gånger vid övergången till vintertid. Välj första eller andra tillfället.");
  return fold === "later" ? candidates.at(-1) : candidates[0];
}
const PIXII_ERRORS = {
  release_gate_closed: "Styrbehörigheten är avstängd. Slå på Production-control authority på enhetssidan och förhandsgranska igen.",
  production_control_disabled: "Styrbehörigheten är avstängd. Slå på Production-control authority och förhandsgranska igen.",
  scheduler_target_changed_create_new_preview: "Schemat har ändrats sedan förhandsgranskningen. Uppdatera listan och förhandsgranska igen.",
  scheduler_list_detail_changed: "Gatewayen gav olika uppgifter om schemat. Uppdatera och försök igen.",
  scheduler_target_missing_or_ambiguous: "Schemat finns inte längre eller kan inte identifieras entydigt. Uppdatera listan.",
  preview_state_changed: "Schemat eller styrningen har ändrats. Gör en ny förhandsgranskning.",
  preview_missing_or_consumed: "Förhandsgranskningen har gått ut eller redan använts. Gör en ny förhandsgranskning.",
  scheduler_ownership_store_invalid: "Schemalagringen är inte redo, exempelvis för att en tidigare åtgärd väntar på efterkontroll.",
  scheduler_ownership_ambiguous: "En tidigare schemaåtgärd behöver kontrolleras innan nya ändringar kan göras.",
  pending_command_operations: "En annan åtgärd pågår. Vänta tills den är klar och uppdatera.",
  control_recovery_required: "En tidigare åtgärd väntar på efterkontroll. Välj Kontrollera resultat i listan.",
  recovery_pending: "Resultatet är fortfarande osäkert. Kontrollera igen utan att skicka samma åtgärd på nytt.",
  recovery_readback_unavailable: "Gatewayens efterkontroll är inte tillgänglig. Åtgärden står kvar som väntande.",
  scheduler_visibility_stale: "Schemainformationen är inte färsk. Uppdatera och förhandsgranska igen.",
  scheduler_unavailable: "Anslutningen till schemaläggaren är inte tillgänglig. Kontrollera integrationen.",
  scheduler_installer_access_required: "Gatewaykontot saknar installationsbehörighet för att ändra scheman.",
  scheduler_no_changes: "Inga inställningar har ändrats.",
  scheduler_edit_below_gateway_minimum: "Start eller längd understiger gatewayens tillåtna minimum.",
  scheduler_create_values_invalid: "Kontrollera tjänstens inställningar. Starten måste vara i framtiden, längden högst sju dygn och batterimålet 0–100 %.",
  scheduler_create_service_unsupported: "Den valda tjänsten stöds inte för nya scheman.",
  scheduler_soc_value_invalid: "Batterimålet måste vara ett tal mellan 0 och 100 %.",
  scheduler_soc_scope_unverified: "Det gick inte att kontrollera vilka scheman som delar batterimålet. Uppdatera och försök igen.",
  active_service_impact_unproven: "En pågående tjänst blockerar för närvarande skapande av nya scheman. Försök när tjänsten har avslutats.",
  scheduler_external_conflict: "Ett befintligt aktiverat schema krockar med tiden, eller har en upprepningsregel som behöver kontrolleras. Välj en annan tid och uppdatera scheman.",
  mqtt_disconnected: "MQTT-anslutningen är bruten. Vänta tills anslutningen fungerar och uppdatera.",
  telemetry_stale: "Batteriets mätvärden är för gamla. Vänta på färska mätvärden och uppdatera.",
  system_not_safe: "Systemets status tillåter inte styrning just nu.",
  battery_not_safe: "Batteriets status tillåter inte styrning just nu.",
  grid_not_safe: "Elnätets status tillåter inte styrning just nu.",
  system_events_not_clear: "Systemet har aktiva händelser som behöver hanteras innan styrning tillåts.",
  protected_service_active: "En skyddad tjänst är aktiv i gatewayen. Vänta tills den har avslutats.",
  evidence_required: "Den här åtgärden saknar verifierat stöd i gatewayen.",
  adapter_capability_unsupported: "Den här schematypen stöds inte för åtgärden.",
  request_invalid: "Kontrollera tider, längd och batterinivå och försök igen.",
};
function pixiiError(error) {
  const raw = typeof error === "string" ? error : error?.message || error?.code || "unknown";
  if (raw.startsWith("Scheduler authentication")) return "Inloggningen till gatewayens schemaläggare fungerar inte. Kontrollera integrationens anslutning och behörighet.";
  return PIXII_ERRORS[raw] || `Åtgärden kunde inte slutföras. Uppdatera och försök igen. Teknisk information: ${raw}`;
}
function pixiiRecurrence(schedule) {
  const rule = schedule.recurrence?.details || {};
  const type = rule.rec_type;
  const recurring = type > 0 || schedule.recurrence?.recurring === true;
  if (type === 0 && !recurring || !recurring && schedule.recurrence?.recurring === false) return {recurring: false, text: "Engångsschema"};
  let text = "Återkommande schema – regel ej fullständigt verifierad";
  if (type === 1) text = `Var ${rule.recurrence1} timme`;
  if (type === 2) text = `Var ${rule.recurrence1}:e dag`;
  if (type === 3) {
    const weekdays = ["söndag", "måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag"].filter((_, i) => (rule.recurrence2 >>> i) & 1);
    text = `Var ${rule.recurrence1}:e vecka · ${weekdays.join(", ") || "inga veckodagar angivna"}`;
  }
  if (type === 4) {
    const months = ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober", "november", "december"].filter((_, i) => (rule.recurrence1 >>> i) & 1);
    const days = Array.from({length: 31}, (_, i) => i + 1).filter(n => (rule.recurrence2 >>> (n - 1)) & 1);
    text = `Månader: ${months.join(", ")} · dagar: ${days.join(", ")}`;
  }
  if (type == null || type > 4) text += ` · regeldata ${JSON.stringify(rule)}`;
  if (rule.rec_end_time === 0) text += " · inget slutdatum";
  if (rule.rec_end_time > 0) text += ` · till ${pixiiClock.format(new Date(rule.rec_end_time * 1000))}`;
  return {recurring: recurring || type == null, text};
}
class PixiihomeControlCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {}; this._entryId = this._config.entry_id || null;
    this._state = null; this._selected = null; this._draft = null; this._preview = null;
    this._error = null; this._notice = null; this._loading = false; this._busy = false; this._render();
  }
  set hass(hass) { this._hass = hass; if (!this._state && !this._loading) this._load(); }
  connectedCallback() {
    clearInterval(this._timer);
    this._timer = setInterval(() => { if (!document.hidden && !this._selected && !this._busy) this._load(); }, 3600000);
  }
  disconnectedCallback() { clearInterval(this._timer); }
  getCardSize() { return 4; }
  async _load() {
    if (!this._hass || this._loading || this._busy) return;
    this._loading = true; this._render();
    try {
      this._state = await this._hass.callWS({type: "pixiihome_controller/panel/state"});
      if (!this._entryId && this._state.entries?.length === 1) this._entryId = this._state.entries[0].entry_id;
      this._error = null;
    } catch (error) { this._error = pixiiError(error); }
    finally { this._loading = false; this._render(); }
  }
  _date(epoch) { return pixiiClock.format(new Date(epoch * 1000)); }
  _length(seconds) { return `${Math.floor(seconds / 3600)} h ${Math.floor(seconds % 3600 / 60)} min${seconds % 60 ? ` ${seconds % 60} s` : ""}`; }
  _open(schedule = null) {
    const start = schedule?.start_epoch ?? Math.ceil((Date.now() / 1000 + 900) / 60) * 60;
    const duration = schedule?.duration_seconds ?? 3600;
    this._selected = schedule || {creating: true};
    this._draft = {start: pixiiLocalTime(start), end: pixiiLocalTime(start + duration),
      hours: Math.floor(duration / 3600), minutes: Math.floor(duration % 3600 / 60), seconds: duration % 60, soc: schedule ? (schedule.target_soc_percent ?? "") : 80, service: "targetsoc", direction: "charge", power: "", exportLimit: 0, importLimit: "", chargeMax: "", dischargeMax: "", keepActive: false, maxChange: "", fold: "", endFold: ""};
    this._preview = null; this._error = null; this._notice = null; this._render();
  }
  _readDraft() {
    for (const key of ["start", "end", "hours", "minutes", "seconds", "soc", "fold", "endFold", "service", "direction", "power", "exportLimit", "importLimit", "chargeMax", "dischargeMax", "keepActive", "maxChange"]) {
      const input = this.querySelector(`#${key}`); if (input) this._draft[key] = input.type === "checkbox" ? input.checked : input.value;
    }
  }
  _invalidatePreview() { this._preview = null; this.querySelector('[aria-label="Förhandsgranskning"]')?.remove(); }
  _syncTimes(changed) {
    this._readDraft(); this._invalidatePreview();
    const d = this._draft;
    try {
      const start = pixiiEpoch(d.start, d.fold, this._selected.start_epoch);
      if (changed === "end" || changed === "endFold") {
        const end = pixiiEpoch(d.end, d.endFold, this._selected.start_epoch + this._selected.duration_seconds);
        if (end <= start) throw new Error("Sluttiden måste vara efter starttiden.");
        const duration = end - start;
        d.hours = Math.floor(duration / 3600); d.minutes = Math.floor(duration % 3600 / 60); d.seconds = duration % 60;
        for (const key of ["hours", "minutes", "seconds"]) this.querySelector(`#${key}`).value = d[key];
      } else {
        const duration = Number(d.hours) * 3600 + Number(d.minutes) * 60 + Number(d.seconds);
        d.end = pixiiLocalTime(start + duration);
        const candidates = pixiiTimeCandidates(d.end);
        d.endFold = candidates.length > 1 ? (candidates[0] === start + duration ? "earlier" : "later") : "";
        this.querySelector("#end").value = d.end; this.querySelector("#endFold").value = d.endFold;
      }
      this.querySelector("#time-error").textContent = "";
    } catch (error) { this.querySelector("#time-error").textContent = error.message; }
    for (const [key, field] of [["fold", "start"], ["endFold", "end"]]) this.querySelector(`#${key}-label`).hidden = pixiiTimeCandidates(d[field]).length < 2;
  }
  async _prepare(operation) {
    if (this._busy || this._loading) return;
    this._readDraft();
    const creating = operation === "create";
    let payload;
    try {
      if (operation !== "delete") {
        if (!this.querySelector("#schedule-edit").reportValidity()) return;
        const d = this._draft;
        const start = pixiiEpoch(d.start, d.fold, this._selected.start_epoch);
        const duration = Number(d.hours) * 3600 + Number(d.minutes) * 60 + Number(d.seconds);
        if (!Number.isInteger(duration) || duration <= 0 || duration > 2147483647) throw new Error("Ange en längd större än noll.");
        const end = pixiiEpoch(d.end, d.endFold, this._selected.start_epoch + this._selected.duration_seconds);
        if (end <= start || end - start !== duration) throw new Error("Kontrollera sluttid och längd. Sluttiden måste vara efter starttiden och stämma med längden.");
        if (creating) {
          if (start <= Date.now() / 1000 || duration > 604800) throw new Error(PIXII_ERRORS.scheduler_create_values_invalid);
          const values = pixiiServiceValues(d);
          payload = {type: "pixiihome_controller/panel/preview", entry_id: this._entryId, operation, service: d.service,
            intent: {start_epoch: start, duration_seconds: duration, ...(d.service === "targetsoc" ? values : {values})}};
        } else {
          const changes = {};
          if (start !== this._selected.start_epoch) changes.start_time = start;
          if (duration !== this._selected.duration_seconds) changes.duration = duration;
          if (this._selected.service_type === 107 && this._selected.target_soc_percent != null && Number(d.soc) !== this._selected.target_soc_percent) changes.socref = Number(d.soc);
          if (!Object.keys(changes).length) throw new Error("Inga inställningar har ändrats.");
          payload = {type: "pixiihome_controller/gateway_scheduler/preview", entry_id: this._entryId,
            sch_index: this._selected.sch_index, operation, changes};
        }
      } else payload = {type: "pixiihome_controller/gateway_scheduler/preview", entry_id: this._entryId,
        sch_index: this._selected.sch_index, operation, changes: {}};
    } catch (error) { this._error = error.message; this._render(); return; }
    this._busy = true; this._error = null; this._notice = null; this._preview = null; this._render();
    try {
      this._preview = await this._hass.callWS(payload);
      if (this._preview.before?.rec_type != null) this._selected = {...this._selected,
        recurrence: {details: this._preview.before, recurring: this._preview.before.rec_type > 0}};
    }
    catch (error) { this._error = pixiiError(error); }
    finally { this._busy = false; this._render(); }
  }
  async _apply() {
    if (!this._preview?.can_apply || !this.querySelector("#confirm")?.checked || this._busy) return;
    const preview = this._preview;
    this._preview = null; this._busy = true; this._render();
    try {
      const creating = preview.operation === "create";
      const result = await this._hass.callWS({type: `pixiihome_controller/${creating ? "panel" : "gateway_scheduler"}/apply`,
        entry_id: this._entryId, preview_id: preview.preview_id, ...(creating ? {} : {confirm_external_target: true})});
      if (result.status === "blocked" || result.status === "rejected") {
        this._error = (result.blocked_reasons?.length ? result.blocked_reasons : [result.reason]).map(pixiiError).join(" ");
      } else {
        this._notice = ["reconciled", "success"].includes(result.status)
          ? "Åtgärden är genomförd och verifierad genom efterkontroll i gatewayen."
          : "Resultatet väntar på efterkontroll. Skicka inte samma åtgärd igen; välj Kontrollera resultat nedan.";
        this._selected = null; this._draft = null;
      }
    } catch (error) {
      this._notice = "Svaret kunde inte hämtas. Uppdatera och kontrollera väntande resultat innan du försöker igen.";
      this._selected = null; this._draft = null;
    } finally { this._busy = false; if (!this._selected) await this._load(); else this._render(); }
  }
  async _recover(transactionId, native) {
    if (this._busy || this._loading) return;
    this._busy = true; this._preview = null; this._render();
    try {
      const result = await this._hass.callWS({type: `pixiihome_controller/${native ? "gateway_scheduler" : "panel"}/recover`, entry_id: this._entryId, transaction_id: transactionId});
      this._notice = result.status === "reconciled" ? "Resultatet är verifierat. Inget kommando skickades på nytt." : PIXII_ERRORS.recovery_pending;
    } catch (error) { this._notice = pixiiError(error); }
    finally { this._busy = false; await this._load(); }
  }
  _closeEditor() {
    if (this._busy) return;
    this._selected = null; this._draft = null; this._preview = null; this._error = null; this._render();
  }
  _previewHTML() {
    const p = this._preview; if (!p) return "";
    const creating = p.operation === "create";
    const summary = r => r ? `${this._date(r.start_time ?? r.start_epoch)} → ${this._date((r.start_time ?? r.start_epoch) + (r.duration ?? r.duration_seconds))} · ${this._length(r.duration ?? r.duration_seconds)}` : "Schemat tas bort permanent";
    return `<section aria-label="Förhandsgranskning"><h3 id="preview-title" tabindex="-1">${creating ? "Skapa schema" : p.operation === "delete" ? "Radera schema" : "Ändra schema"}${creating ? "" : ` #${p.sch_index}`}</h3>
      ${creating ? "" : `<p>Före: ${this._escape(summary(p.before))}</p>`}<p>${creating ? "Nytt schema" : "Efter"}: ${this._escape(summary(p.after))}${creating ? ` · ${this._escape(PIXII_SERVICES[p.service])} · ${this._escape(pixiiServiceSummary(p.service, p.service === "targetsoc" ? {socref:p.after.socref} : p.after.values))} · engångsschema` : ""}</p>
      ${!creating && p.before?.service_type === 107 ? `<p>Batterimål: ${p.service_before?.reference ?? this._selected.target_soc_percent ?? "okänt"} % → ${p.operation === "delete" ? "schemat tas bort" : `${p.target_soc_percent ?? p.service_before?.reference ?? this._selected.target_soc_percent ?? "okänt"} %`}</p>` : ""}
      ${p.target_soc_percent != null ? `<p class="error">Måländringen gäller den underliggande Target SoC-inställningen. Berörda scheman: ${p.affected_soc_schedules.map(t=>`#${t.sch_index}`).join(", ")}. Om ett av dem pågår kan batteriets mål ändras direkt. Övriga scheman behåller sina tider.</p>` : ""}
      ${(p.blocked_reasons || []).length ? `<ul class="error">${p.blocked_reasons.map(r => `<li>${this._escape(pixiiError(r))}</li>`).join("")}</ul>` : ""}
      ${p.can_apply ? `<label class="confirm"><input id="confirm" type="checkbox">Jag godkänner ${creating ? "detta nya schema" : "denna exakta åtgärd, även för ett schema skapat av Greenely"}.</label><p class="muted">Godkännandet gäller i två minuter och kan användas en gång.</p><button type="button" id="apply" disabled>${creating ? "Skapa schema" : p.operation === "delete" ? "Radera schemat permanent" : "Spara ändringen"}</button>` : ""}</section>`;
  }
  _serviceFields(creating, disabled) {
    const d = this._draft;
    const number = (key, label, min = "") => `<label>${label}<input id="${key}" type="number" ${min === "" ? "" : `min="${min}"`} step="0.001" required value="${this._escape(d[key])}" ${disabled}></label>`;
    if (!creating) return this._selected.service_type === 107 ? `<label>Batterimål (%)<input id="soc" type="number" min="0" max="100" step="0.1" required value="${this._escape(d.soc)}" ${this._selected.target_soc_percent == null ? "disabled" : disabled}></label>${this._selected.target_soc_percent == null ? `<p class="error">Mål-SOC kunde inte hämtas. Uppdatera scheman och öppna formuläret igen.</p>` : ""}` : "";
    const service = `<label>Tjänst<select id="service" ${disabled}>${Object.entries(PIXII_SERVICES).map(([key,label])=>`<option value="${key}" ${d.service===key?"selected":""}>${label}</option>`).join("")}</select></label>`;
    if (d.service === "targetsoc") return `${service}<label>Batterimål (%)<input id="soc" type="number" min="0" max="100" step="0.1" required value="${this._escape(d.soc)}" ${disabled}></label>`;
    if (d.service === "demandresponse") return `${service}<label>Riktning<select id="direction" ${disabled}><option value="charge" ${d.direction==="charge"?"selected":""}>Ladda batteriet</option><option value="discharge" ${d.direction==="discharge"?"selected":""}>Ladda ur batteriet</option></select></label>${number("power","Effekt (kW)",0.001)}<p class="muted">Effekten avser nät-/växelströmssidan. Batteriets tillåtna gränser gäller fortfarande.</p>`;
    return `${service}<p class="muted">Gränserna avser elmätarens nettoeffekt. Positivt värde är import, negativt värde är export. Batteriet laddar under laddningsgränsen och laddar ur över urladdningsgränsen.</p>${number("exportLimit","Laddningsgräns vid elmätaren (kW)")}${number("importLimit","Urladdningsgräns vid elmätaren (kW)")}${number("chargeMax","Max laddning (kW)",0)}${number("dischargeMax","Max urladdning (kW)",0)}${d.service === "adaptivepeakshaving" ? number("maxChange","Max ändring av gränser (kW)",0) : ""}<label class="confirm"><input id="keepActive" type="checkbox" ${d.keepActive?"checked":""} ${disabled}>Behåll tjänsten aktiv mellan gränserna</label><p class="muted">När detta är valt får tjänster med lägre prioritet inte ta över mellan laddnings- och urladdningsgränsen.</p>`;
  }
  _render() {
    const oldDialog = this.querySelector("dialog"); const active = this.getRootNode().activeElement;
    const focusId = oldDialog?.contains(active) ? active.id : null;
    const scroll = this.querySelector(".dialog-body")?.scrollTop || 0;
    const hadPreview = !!this.querySelector('[aria-label="Förhandsgranskning"]');
    if (oldDialog?.open) oldDialog.close();
    const entry = this._state?.entries?.find(e => e.entry_id === this._entryId);
    const authority = entry?.release_gate?.production_control_enabled;
    const disabled = this._busy || this._loading ? "disabled" : "";
    const evidence = entry?.scheduler_evidence || {};
    const stale = evidence.invalidated === true || evidence.fresh === false || evidence.list_visibility_fresh === false;
    const nowEpoch = Date.now() / 1000; const endOf = s => s.end_epoch ?? s.start_epoch + s.duration_seconds; const schedules = [...(entry?.schedules || [])].sort((a, b) => ((endOf(a) <= nowEpoch) - (endOf(b) <= nowEpoch)) || (a.start_epoch - b.start_epoch)); const d = this._draft; const creating = this._selected?.creating;
    const recurrence = this._selected && !creating ? pixiiRecurrence(this._selected) : null;
    const dateInput = (key, label) => `<label for="${key}">${label}</label><input id="${key}" type="datetime-local" step="1" required value="${this._escape(d[key])}" ${disabled}>`;
    const foldSelect = (key, field) => `<label id="${key}-label" ${pixiiTimeCandidates(d[field]).length < 2 ? "hidden" : ""}>Tid vid vintertidsövergång<select id="${key}" ${disabled}><option value="">Välj tillfälle</option><option value="earlier" ${d[key] === "earlier" ? "selected" : ""}>Första tillfället (sommartid)</option><option value="later" ${d[key] === "later" ? "selected" : ""}>Andra tillfället (vintertid)</option></select></label>`;
    this.innerHTML = `<ha-card header="Pixii scheman"><div class="content">
      <div class="toolbar"><label>Gateway<select id="entry" ${disabled}>${(this._state?.entries || []).map(e => `<option value="${this._escape(e.entry_id)}" ${e.entry_id === this._entryId ? "selected" : ""}>${this._escape(e.gateway_serial || e.serial || e.entry_id)}</option>`).join("") || "<option>Ingen gateway tillgänglig</option>"}</select></label><button id="refresh" type="button" ${disabled}>${this._loading ? "Hämtar scheman…" : "Uppdatera scheman"}</button>${entry?.schedule_create_available ? `<button id="create" type="button" ${disabled}>Nytt schema</button>` : ""}</div>
      <p class="muted">Europe/Stockholm · Automatisk uppdatering varje timme${this._selected ? " (pausad medan formuläret är öppet)" : ""}.</p>
      <p class="${stale ? "error" : "muted"}" role="status">${stale ? "Schemainformationen kunde inte uppdateras. Försök med Uppdatera scheman." : evidence.last_successful_at ? `Senast hämtat: ${this._escape(this._date(evidence.last_successful_at))}` : ""}${evidence.from_epoch != null ? `<br>Visat intervall: ${this._escape(this._date(evidence.from_epoch))} – ${this._escape(this._date(evidence.to_epoch))}` : ""}</p>
      <p class="status ${authority ? "open" : "closed"}">Styrbehörighet ${authority ? "på" : "av"}</p>
      ${(entry?.recovery || []).filter(r => r.operation?.startsWith("native_scheduler_") || r.operation === "create").map(r => `<section><h3>Väntande efterkontroll</h3><p>${r.operation === "create" ? "Nytt schema" : `Schema #${this._escape(r.target_identity.sch_index)}`}: resultatet är ännu inte verifierat.</p><button type="button" data-recovery="${this._escape(r.transaction_id)}" data-native="${r.operation.startsWith("native_scheduler_")}" ${disabled}>Kontrollera resultat (endast läsning)</button></section>`).join("")}
      <h3>Scheman (${schedules.filter(s => endOf(s) > nowEpoch).length} kommande, ${schedules.filter(s => endOf(s) <= nowEpoch).length} avslutade)</h3><ul class="schedules">${schedules.map(s => `<li><strong>${this._escape(s.service_type_label || "Schema")} #${s.sch_index}</strong><br>${this._escape(this._date(s.start_epoch))} → ${this._escape(this._date(s.end_epoch ?? s.start_epoch + s.duration_seconds))}<br>${this._escape(this._length(s.duration_seconds))}${s.service_type === 107 ? ` · Mål-SOC: ${s.target_soc_percent != null ? `${s.target_soc_percent} %` : "kunde inte hämtas"}` : ""} · ${endOf(s) <= nowEpoch ? "Avslutat" : s.enabled === false ? "Inaktiverat" : "Aktiverat"}<br><span class="muted">${this._escape(pixiiRecurrence(s).text)}<br>Källa ${this._escape(s.source_id ?? "okänd")} · ${this._escape(s.owner_label || s.source_label || ({integration_owned:"Skapat av integrationen",external:"Extern",unknown:"Okänt ursprung"}[s.origin]) || "Okänt ursprung")}</span>${entry.gateway_scheduler_available ? `<button type="button" data-schedule="${s.sch_index}" ${disabled}>Hantera schema #${s.sch_index}</button>` : ""}</li>`).join("") || `<li>${stale ? "Ingen färsk schemainformation." : "Inga scheman i det hämtade intervallet."}</li>`}</ul>
      ${this._selected ? `<dialog class="schedule-dialog" aria-labelledby="schedule-title"><header class="dialog-header"><h3 id="schedule-title" tabindex="-1">${creating ? "Nytt schema" : `Schema #${this._selected.sch_index}`}</h3><button id="cancel" type="button" ${this._busy ? "disabled" : ""}>Stäng</button></header><div class="dialog-body">
        <p>${creating ? "Välj tjänst och inställningar för den angivna tiden. Det nya schemat gäller en gång." : "Ändringen gäller schemat i gatewayen, även om Greenely har skapat det. Greenely kan senare ändra eller återskapa scheman."}</p>
        ${recurrence ? `<p><strong>${this._escape(recurrence.text)}</strong>${recurrence.recurring ? "<br>Ändring eller radering gäller hela den återkommande regeln och dess framtida tillfällen, inte bara ett enskilt tillfälle. Tidsändringar bevarar upprepningsregeln." : ""}</p>` : ""}
        <form id="schedule-edit"><p class="muted">Alla tider anges i Europe/Stockholm. Längden är faktisk förfluten tid, även vid sommar-/vintertid.</p>${dateInput("start","Start")}${foldSelect("fold","start")}${dateInput("end","Sluttid")}${foldSelect("endFold","end")}
        <fieldset><legend>Längd</legend><div class="duration">${[["hours","Timmar",596523],["minutes","Minuter",59],["seconds","Sekunder",59]].map(([key,label,max]) => `<label>${label}<input id="${key}" type="number" min="0" max="${max}" step="1" required value="${this._escape(d[key])}" ${disabled}></label>`).join("")}</div></fieldset><p id="time-error" class="error" role="alert"></p>
        ${this._serviceFields(creating, disabled)}<div class="actions"><button id="preview-edit" type="button" ${disabled}>${creating ? "Förhandsgranska nytt schema" : "Förhandsgranska ändring"}</button>${creating ? "" : `<button id="preview-delete" type="button" ${disabled}>Förhandsgranska radering</button>`}</div></form>${this._previewHTML()}${this._error ? `<p class="error" role="alert">${this._escape(this._error)}</p>` : ""}<p role="status">${this._busy ? "Arbetar…" : ""}</p><button id="dialog-refresh" type="button" ${disabled}>Uppdatera schemainformation</button></div></dialog>` : ""}
      ${!this._selected && this._notice ? `<p role="status">${this._escape(this._notice)}</p>` : ""}${!this._selected && this._error ? `<p class="error" role="alert">${this._escape(this._error)}</p>` : ""}</div></ha-card><style>
      .content {padding:16px;font-variant-numeric:tabular-nums} .toolbar,.actions {display:flex;flex-wrap:wrap;gap:8px;align-items:end}.toolbar label {flex:1 1 180px} label {display:grid;gap:6px;font-weight:600}[hidden] {display:none!important}
      select,button,input {min-height:44px;padding:8px;font:inherit;box-sizing:border-box;max-width:100%;color:var(--primary-text-color);background:var(--card-background-color);border:1px solid var(--divider-color,#8b938c);border-radius:6px} button {cursor:pointer}button:disabled {cursor:default;opacity:.6}button:not(:disabled):hover {background:var(--secondary-background-color,#e9e1d4)}
      :is(button,input,select):focus-visible {outline:2px solid var(--primary-color,#236b8e);outline-offset:2px}input {caret-color:var(--primary-color,#236b8e)}::selection {background:var(--primary-color,#236b8e);color:var(--text-primary-color,#fff)}p {line-height:1.5;overflow-wrap:anywhere}h3 {margin-block:20px 8px}.muted {color:var(--secondary-text-color)}.error {color:var(--error-color,#b32e25)}
      .status {padding:8px 10px;border-radius:6px;font-weight:700}.open {color:#0d4d2a;background:#d8f0df}.closed {color:#6b2a24;background:#f5dcd8}.schedules {padding:0;list-style:none}.schedules li {border-top:1px solid var(--divider-color,#cfc6b8);padding-block:12px;line-height:1.5;overflow-wrap:anywhere}li button {display:block;margin-top:8px}section {border-top:1px solid var(--divider-color,#cfc6b8);margin-top:24px;padding-top:12px}.actions {margin-top:16px}.confirm {display:flex;align-items:center;gap:8px}
      .schedule-dialog {padding:0;border:0;border-radius:12px;width:min(640px,calc(100vw - 32px));max-width:calc(100vw - 32px);max-height:calc(100dvh - 32px);box-sizing:border-box;color:var(--primary-text-color,#202724);background:var(--card-background-color,#fff);box-shadow:0 12px 40px #0006;overflow:hidden}.schedule-dialog[open] {display:flex;flex-direction:column}.schedule-dialog::backdrop {background:#0009}.dialog-header {display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 20px;border-bottom:1px solid var(--divider-color,#cfc6b8)}.dialog-header h3 {margin:0;overflow-wrap:anywhere}.dialog-body {padding:0 20px 20px;overflow-y:auto;overscroll-behavior:contain;scrollbar-color:var(--secondary-text-color,#69756e) var(--card-background-color,#fff)}
      #schedule-edit>input {width:100%;margin:6px 0 16px}fieldset {padding:12px 0;border:0;margin:0}.duration {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.duration input {width:100%}#schedule-edit>label {margin-block:12px}#schedule-edit>label input,#schedule-edit>label select {width:100%}@media(max-width:480px){.dialog-header {padding:12px 16px}.dialog-body {padding:0 16px 16px}.actions button {flex:1 1 100%}.toolbar button {flex:1 1 auto}.duration {gap:8px}}
      </style>`;
    this.querySelector("#entry")?.addEventListener("change",e=>{this._entryId=e.target.value;this._closeEditor();this._notice=null;this._render();});
    this.querySelector("#refresh")?.addEventListener("click",()=>{if(this._selected)this._readDraft();this._preview=null;this._load();});
    this.querySelector("#create")?.addEventListener("click",()=>{this._returnSchedule="create";this._open();});
    this.querySelectorAll("[data-schedule]").forEach(b=>b.addEventListener("click",()=>{this._returnSchedule=b.dataset.schedule;this._open(schedules.find(s=>String(s.sch_index)===b.dataset.schedule));}));
    this.querySelectorAll("[data-recovery]").forEach(b=>b.addEventListener("click",()=>this._recover(b.dataset.recovery,b.dataset.native==="true")));
    this.querySelector("#schedule-edit")?.addEventListener("submit",e=>e.preventDefault());
    this.querySelector("#schedule-edit")?.addEventListener("input",e=>{
      if(e.target.id === "service"){this._readDraft();this._preview=null;this._error=null;this._render();}
      else this._syncTimes(e.target.id);
    });
    this.querySelector("#preview-edit")?.addEventListener("click",()=>this._prepare(creating?"create":"update"));
    this.querySelector("#preview-delete")?.addEventListener("click",()=>this._prepare("delete"));
    this.querySelector("#confirm")?.addEventListener("change",e=>{this.querySelector("#apply").disabled=!e.target.checked;});
    this.querySelector("#apply")?.addEventListener("click",()=>this._apply());
    this.querySelector("#cancel")?.addEventListener("click",()=>this._closeEditor());
    this.querySelector("#dialog-refresh")?.addEventListener("click",()=>{this._readDraft();this._preview=null;this._load();});
    const dialog=this.querySelector("dialog");
    if(dialog&&this.isConnected){
      dialog.addEventListener("cancel",e=>{e.preventDefault();this._closeEditor();});dialog.showModal();
      const target=this.querySelector(`#${this._preview&&!hadPreview?"preview-title":focusId||"schedule-title"}`);
      (target&&!target.disabled?target:this.querySelector("#schedule-title")).focus({preventScroll:true});
      this.querySelector(".dialog-body").scrollTop=scroll;
      if(this._preview&&!hadPreview)requestAnimationFrame(()=>{
        const body=this.querySelector(".dialog-body"),section=this.querySelector('[aria-label="Förhandsgranskning"]');
        if(body&&section)body.scrollTop+=section.getBoundingClientRect().top-body.getBoundingClientRect().top-12;
      });
    }else if(oldDialog&&this._returnSchedule){
      const button=this._returnSchedule==="create"?this.querySelector("#create"):[...this.querySelectorAll("[data-schedule]")].find(b=>b.dataset.schedule===this._returnSchedule);
      (button||this.querySelector("#refresh"))?.focus({preventScroll:true});this._returnSchedule=null;
    }
  }
  _escape(value){return String(value).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);}
}
customElements.define("pixiihome-control-card",PixiihomeControlCard);
window.customCards=window.customCards||[];
window.customCards.push({type:"pixiihome-control-card",name:"Pixiihome Controller",description:"Pixii scheman, förhandsgranskning och efterkontroll"});
