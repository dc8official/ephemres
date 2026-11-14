<template>
  <!-- Settings → Data collection → SNMP (v3.3.6 build plan 8.3). The switch and the interval wait
       for the page's shared Save bar; credential sets save when their dialog is confirmed, like
       users and alert channels. Secrets are write-only: the API only says whether each is set. -->
  <div class="snmp-pane">
    <div class="settings-card">
      <div class="card-header">
        <h2 class="card-title">📟 SNMP polling</h2>
        <label class="switch" title="Poll endpoints over SNMP">
          <input type="checkbox" v-model="settings.snmpEnabled" aria-label="SNMP polling" />
          <span class="slider round"></span>
        </label>
      </div>
      <p class="card-desc">
        Reads interface names, speeds and up/down status from each endpoint that has a credential
        set, and notices reboots. Names and speeds typed in the interface editor still win. Give an
        endpoint a set in its form, or many at once from the dashboard's selection bar.
      </p>
      <div class="setting-row">
        <div>
          <label class="setting-label" for="snmp-interval">Poll every</label>
          <p class="setting-hint">Each device's status is read on this interval, spread over it. Names and speeds are re-read when the device reports a change, and every 15 minutes.</p>
        </div>
        <select id="snmp-interval" v-model.number="settings.snmpPollSeconds" class="form-select font-mono tnum">
          <option :value="30">30 seconds</option>
          <option :value="60">1 minute (default)</option>
          <option :value="120">2 minutes</option>
          <option :value="300">5 minutes</option>
        </select>
      </div>
    </div>

    <div class="settings-card">
      <div class="card-header">
        <h2 class="card-title">🔑 SNMP credential sets</h2>
        <button class="btn-secondary" @click="openNew">+ Add credential set</button>
      </div>
      <p class="card-desc">
        Shared sets, such as one per vendor or site. A set holds the community (v2c) or the v3 user
        and its protocols. Use <strong>Test</strong> to check a set against a device before using it.
      </p>

      <div v-if="loading" class="muted">Loading…</div>
      <div v-else-if="!sets.length" class="muted">No credential sets yet.</div>
      <table v-else class="sets-table">
        <thead>
          <tr><th scope="col">Name</th><th scope="col">Security</th><th scope="col">Port</th><th scope="col">Endpoints</th><th scope="col"></th></tr>
        </thead>
        <tbody>
          <tr v-for="s in sets" :key="s.id">
            <td class="font-bold">{{ s.name }}</td>
            <td>
              <span class="chip">{{ securityLabel(s) }}</span>
              <span v-if="s.version === '3'" class="muted small"> user {{ s.username }}</span>
              <span v-if="missingSecret(s)" class="warn small"> · secret missing</span>
            </td>
            <td class="font-mono tnum">{{ s.port }}</td>
            <td class="tnum">{{ s.endpoints }}</td>
            <td class="row-actions">
              <template v-if="confirmDelete === s.id">
                <span class="small">Delete{{ s.endpoints ? `, and stop polling ${s.endpoints} endpoint${s.endpoints === 1 ? '' : 's'}` : '' }}?</span>
                <button class="btn-danger small-btn" @click="remove(s)">Delete</button>
                <button class="btn-secondary small-btn" @click="confirmDelete = null">Keep</button>
              </template>
              <template v-else>
                <button class="btn-secondary small-btn" @click="openEdit(s)">Edit</button>
                <button class="btn-secondary small-btn" @click="confirmDelete = s.id">Delete</button>
              </template>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="dialogOpen" class="modal-overlay" @click.self="closeDialog">
      <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="snmp-set-title">
        <div class="modal-header">
          <h3 id="snmp-set-title" class="modal-title">{{ form.id ? 'Edit credential set' : 'New credential set' }}</h3>
          <button class="close-btn" aria-label="Close" @click="closeDialog">×</button>
        </div>

        <form class="modal-form" @submit.prevent="save">
          <label class="field"><span>Name</span>
            <input v-model.trim="form.name" required maxlength="64" placeholder="e.g. Branch v3, Meraki" />
          </label>

          <div class="field"><span>Version</span>
            <div class="seg">
              <label><input type="radio" value="2c" v-model="form.version" /> v2c</label>
              <label><input type="radio" value="3" v-model="form.version" /> v3 (recommended)</label>
            </div>
          </div>

          <template v-if="form.version === '2c'">
            <label class="field"><span>Community</span>
              <input v-model="form.community" type="password" autocomplete="new-password"
                     :placeholder="form.has_community ? 'Stored: leave empty to keep' : ''" />
            </label>
          </template>

          <template v-else>
            <label class="field"><span>User name</span>
              <input v-model.trim="form.username" required maxlength="64" autocomplete="off" />
            </label>
            <div class="row2">
              <label class="field"><span>Authentication</span>
                <select v-model="form.auth_protocol" class="form-select">
                  <option :value="null">None</option>
                  <option v-for="a in AUTH" :key="a.value" :value="a.value">{{ a.label }}</option>
                </select>
              </label>
              <label v-if="form.auth_protocol" class="field"><span>Authentication password</span>
                <input v-model="form.auth_password" type="password" autocomplete="new-password"
                       :placeholder="form.has_auth_password ? 'Stored: leave empty to keep' : 'At least 8 characters'" />
              </label>
            </div>
            <div class="row2" v-if="form.auth_protocol">
              <label class="field"><span>Privacy (encryption)</span>
                <select v-model="form.priv_protocol" class="form-select">
                  <option :value="null">None</option>
                  <option v-for="p in PRIV" :key="p.value" :value="p.value">{{ p.label }}</option>
                </select>
              </label>
              <label v-if="form.priv_protocol && !form.priv_same" class="field"><span>Privacy password</span>
                <input v-model="form.priv_password" type="password" autocomplete="new-password"
                       :placeholder="form.has_priv_password ? 'Stored: leave empty to keep' : 'At least 8 characters'" />
              </label>
            </div>
            <label v-if="form.priv_protocol" class="check">
              <input type="checkbox" v-model="form.priv_same" />
              Use the authentication password for privacy too (Meraki and UniFi need this)
            </label>
            <p v-if="form.priv_protocol === 'aes256' || form.priv_protocol === 'aes192'" class="hint">
              net-snmp-style keys. Cisco, HPE Aruba AOS-S and FortiGate "aes256cisco" use the Cisco-style
              option instead; if a test gets no reply after the device answered discovery, try the other.
            </p>
          </template>

          <details class="advanced">
            <summary>Advanced</summary>
            <div class="row3">
              <label class="field"><span>Port</span><input v-model.number="form.port" type="number" min="1" max="65535" /></label>
              <label class="field"><span>Timeout (ms)</span><input v-model.number="form.timeout_ms" type="number" min="500" max="10000" step="500" /></label>
              <label class="field"><span>Retries</span><input v-model.number="form.retries" type="number" min="0" max="3" /></label>
            </div>
            <label v-if="form.version === '3'" class="field"><span>Context name (leave empty unless the device needs one)</span>
              <input v-model.trim="form.context" maxlength="64" />
            </label>
          </details>

          <div class="test-box">
            <div class="test-row">
              <input v-model.trim="testAddress" placeholder="Device address to test, e.g. 10.20.0.1" aria-label="Device address to test" />
              <button type="button" class="btn-secondary" :disabled="testing || !testAddress" @click="runTest">
                {{ testing ? 'Testing…' : 'Test' }}
              </button>
            </div>
            <div v-if="testResult" class="test-result" :class="testResult.ok ? 'ok' : 'bad'">
              <strong>{{ testResult.ok ? 'The device answered.' : 'No good: ' + (testResult.error || 'see below') }}</strong>
              <ul v-if="testResult.findings && testResult.findings.length">
                <li v-for="f in testResult.findings" :key="f">{{ f }}</li>
              </ul>
              <details><summary>Full report</summary><pre>{{ testResult.text }}</pre></details>
            </div>
          </div>

          <p v-if="formError" class="form-error" role="alert">{{ formError }}</p>
          <div class="modal-actions">
            <button type="button" class="btn-secondary" @click="closeDialog">Cancel</button>
            <button type="submit" class="btn-primary" :disabled="savingSet">{{ savingSet ? 'Saving…' : 'Save set' }}</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useEscapeClose } from '../../composables/useEscapeClose.js'
import { useToast } from 'primevue/usetoast'
import {
  getSnmpCredentials, createSnmpCredentials, updateSnmpCredentials, deleteSnmpCredentials, testSnmpCredentials,
} from '../../services/api.js'

defineProps({ settings: { type: Object, required: true } })

const toast = useToast()
const AUTH = [
  { value: 'sha', label: 'SHA (SHA-1)' }, { value: 'sha256', label: 'SHA-256' }, { value: 'sha512', label: 'SHA-512' },
  { value: 'sha224', label: 'SHA-224' }, { value: 'sha384', label: 'SHA-384' }, { value: 'md5', label: 'MD5 (weak)' },
]
const PRIV = [
  { value: 'aes', label: 'AES-128' },
  { value: 'aes256c', label: 'AES-256, Cisco-style keys' }, { value: 'aes192c', label: 'AES-192, Cisco-style keys' },
  { value: 'aes256', label: 'AES-256, net-snmp-style keys' }, { value: 'aes192', label: 'AES-192, net-snmp-style keys' },
  { value: '3des', label: '3DES (weak)' }, { value: 'des', label: 'DES (weak)' },
]

const sets = ref([])
const loading = ref(true)
const confirmDelete = ref(null)
const dialogOpen = ref(false)
const savingSet = ref(false)
const formError = ref('')
const testAddress = ref('')
const testing = ref(false)
const testResult = ref(null)

const blank = () => ({
  id: null, name: '', version: '3', username: '', auth_protocol: 'sha', priv_protocol: 'aes', priv_same: false,
  context: '', port: 161, timeout_ms: 2000, retries: 1, community: '', auth_password: '', priv_password: '',
  has_community: false, has_auth_password: false, has_priv_password: false,
})
const form = reactive(blank())

function securityLabel(s) {
  if (s.version === '2c') return 'v2c'
  const p = PRIV.find((x) => x.value === s.priv_protocol)
  return s.priv_protocol ? `v3 ${(s.auth_protocol || '').toUpperCase()} + ${p ? p.label : s.priv_protocol}`
    : s.auth_protocol ? `v3 ${s.auth_protocol.toUpperCase()}, no encryption` : 'v3, no authentication'
}

function missingSecret(s) {
  if (s.version === '2c') return !s.has_community
  return (s.auth_protocol && !s.has_auth_password) || (s.priv_protocol && !s.priv_same && !s.has_priv_password)
}

async function load() {
  loading.value = true
  try {
    const res = await getSnmpCredentials()
    sets.value = res.data?.data || []
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Could not load SNMP credential sets', detail: e.message, life: 5000 })
  } finally {
    loading.value = false
  }
}

function openNew() {
  Object.assign(form, blank())
  formError.value = ''
  testResult.value = null
  dialogOpen.value = true
}

function openEdit(s) {
  Object.assign(form, blank(), s, { community: '', auth_password: '', priv_password: '' })
  formError.value = ''
  testResult.value = null
  dialogOpen.value = true
}

function closeDialog() {
  dialogOpen.value = false
}

function payload() {
  const body = {
    name: form.name, version: form.version, port: form.port, timeout_ms: form.timeout_ms, retries: form.retries,
  }
  if (form.version === '2c') {
    if (form.community) body.community = form.community
  } else {
    Object.assign(body, {
      username: form.username, auth_protocol: form.auth_protocol, priv_protocol: form.auth_protocol ? form.priv_protocol : null,
      priv_same: !!form.priv_same, context: form.context || '',
    })
    if (form.auth_password) body.auth_password = form.auth_password
    if (form.priv_password && !form.priv_same) body.priv_password = form.priv_password
  }
  return body
}

function errorText(e) {
  const d = e?.response?.data?.detail ?? e?.detail
  if (Array.isArray(d)) return d.map((x) => x.msg || String(x)).join('; ')
  return d || e?.message || 'Request failed'
}

async function save() {
  savingSet.value = true
  formError.value = ''
  try {
    if (form.id) await updateSnmpCredentials(form.id, payload())
    else await createSnmpCredentials(payload())
    toast.add({ severity: 'success', summary: 'Credential set saved', life: 3000 })
    dialogOpen.value = false
    await load()
  } catch (e) {
    formError.value = errorText(e)
  } finally {
    savingSet.value = false
  }
}

async function remove(s) {
  try {
    const res = await deleteSnmpCredentials(s.id)
    const n = res.data?.data?.endpoints_turned_off || 0
    toast.add({ severity: 'success', summary: `Deleted ${s.name}`, detail: n ? `${n} endpoint(s) are no longer polled.` : undefined, life: 4000 })
    confirmDelete.value = null
    await load()
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Could not delete', detail: errorText(e), life: 5000 })
  }
}

async function runTest() {
  testing.value = true
  testResult.value = null
  try {
    // A stored set with no changed secrets is tested as stored; otherwise the form's values.
    const body = form.id && !form.community && !form.auth_password && !form.priv_password
      ? { address: testAddress.value, credential_id: form.id }
      : { address: testAddress.value, credential: payload() }
    const res = await testSnmpCredentials(body)
    testResult.value = res.data?.data
  } catch (e) {
    testResult.value = { ok: false, error: errorText(e), findings: [], text: '' }
  } finally {
    testing.value = false
  }
}

useEscapeClose(dialogOpen, () => closeDialog())

onMounted(load)
</script>

<style scoped>
.snmp-pane { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 20px; margin-top: 20px; }
.settings-card { background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 8px; padding: 20px; }
.card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; gap: 12px; }
.card-title { font-size: 16px; font-weight: 700; color: var(--text-primary); margin: 0; }
.card-desc { font-size: 13px; color: var(--text-muted); line-height: 1.5; margin: 0 0 16px 0; }
.setting-row { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 16px 0; border-top: 1px solid var(--border-color); }
.setting-label { font-size: 14px; font-weight: 600; color: var(--text-primary); margin: 0 0 4px 0; }
.setting-hint, .muted { font-size: 12px; color: var(--text-muted); margin: 0; }
.small { font-size: 12px; }
.warn { color: #f59e0b; }
.form-select, .modal-form input:not([type=radio]):not([type=checkbox]) {
  background: var(--bg-surface-selected); border: 1px solid var(--border-color); color: var(--text-primary);
  padding: 6px 10px; border-radius: 6px; font-size: 13px; width: 100%; box-sizing: border-box;
}
.form-select:focus, .modal-form input:focus { border-color: #0ea5e9; }
#snmp-interval { width: auto; }
.switch { position: relative; display: inline-block; width: 44px; height: 24px; flex-shrink: 0; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider { position: absolute; cursor: pointer; inset: 0; background-color: rgba(255, 255, 255, 0.15); transition: 0.2s; border-radius: 24px; }
.slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; transition: 0.2s; border-radius: 50%; }
input:checked + .slider { background-color: #10b981; }
input:checked + .slider:before { transform: translateX(20px); }
.sets-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.sets-table th { text-align: left; color: var(--text-muted); font-weight: 600; font-size: 12px; padding: 6px 8px; border-bottom: 1px solid var(--border-color); }
.sets-table td { padding: 8px; border-bottom: 1px solid var(--border-color); vertical-align: middle; }
.row-actions { text-align: right; white-space: nowrap; }
.row-actions > * { margin-left: 6px; }
.chip { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; background: var(--bg-surface-selected); border: 1px solid var(--border-color); }
.btn-primary, .btn-secondary, .btn-danger { border-radius: 6px; padding: 6px 12px; font-size: 13px; cursor: pointer; border: 1px solid var(--border-color); }
.btn-primary { background: #0ea5e9; border-color: #0ea5e9; color: #fff; font-weight: 600; }
.btn-secondary { background: var(--bg-surface-selected); color: var(--text-primary); }
.btn-danger { background: #dc2626; border-color: #dc2626; color: #fff; }
.small-btn { padding: 3px 8px; font-size: 12px; }
button:disabled { opacity: 0.6; cursor: not-allowed; }
.modal-overlay { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.55); display: flex; align-items: flex-start; justify-content: center; z-index: 1000; overflow-y: auto; padding: 40px 12px; }
.modal-card { background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 10px; width: min(620px, 100%); padding: 20px; }
.modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.modal-title { margin: 0; font-size: 17px; }
.close-btn { background: none; border: none; color: var(--text-muted); font-size: 22px; cursor: pointer; }
.modal-form { display: flex; flex-direction: column; gap: 12px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-muted); flex: 1; }
.seg { display: flex; gap: 16px; font-size: 13px; color: var(--text-primary); }
.row2, .row3 { display: flex; gap: 12px; }
.check { font-size: 13px; color: var(--text-primary); display: flex; gap: 8px; align-items: center; }
.hint { font-size: 12px; color: var(--text-muted); margin: 0; }
.advanced summary { cursor: pointer; font-size: 13px; color: var(--text-muted); }
.advanced[open] { display: flex; flex-direction: column; gap: 10px; }
.test-box { border: 1px dashed var(--border-color); border-radius: 8px; padding: 10px; display: flex; flex-direction: column; gap: 8px; }
.test-row { display: flex; gap: 8px; }
.test-result { font-size: 13px; }
.test-result.ok strong { color: #10b981; }
.test-result.bad strong { color: #f87171; }
.test-result ul { margin: 6px 0; padding-left: 18px; color: var(--text-muted); }
.test-result pre { white-space: pre-wrap; font-size: 12px; max-height: 260px; overflow: auto; background: var(--bg-surface-selected); padding: 8px; border-radius: 6px; }
.form-error { color: #f87171; font-size: 13px; margin: 0; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; }
.font-mono { font-family: var(--font-mono); }
.font-bold { font-weight: 700; }
.tnum { font-variant-numeric: tabular-nums; }
</style>
