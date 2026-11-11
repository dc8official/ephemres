// Tells the server the user is really using the app, so their session's idle timeout restarts
// (POST /auth/activity, v3.3.6). Only clicks, taps, key presses and page navigation count:
// dashboard polling and the live-event stream must not keep an unattended screen signed in.
//
// At most one report a minute. The first interaction after a quiet minute is sent at once; later
// ones in the same minute are folded into one report at the end of that minute, so the last
// interaction always reaches the server within a minute of happening. The shortest idle
// timeout is 15 minutes, so a minute of lag never signs anyone out early.
import { recordActivity } from '../services/api.js'
import { currentUser } from '../services/auth.js'

const REPORT_INTERVAL_MS = 60 * 1000
// Pages where nobody is signed in yet, so there is no session to keep alive.
const SIGNED_OUT_PATHS = ['/login']

let lastSentAt = 0
let lastInteractionAt = 0
let pendingTimer = null
let inFlight = false

function signedIn() {
  return !!currentUser.value && !SIGNED_OUT_PATHS.includes(window.location.pathname)
}

async function send() {
  if (inFlight || !signedIn()) return
  inFlight = true
  lastSentAt = Date.now()
  try {
    await recordActivity()
  } catch {
    // A 401 has already sent the browser to the sign-in page with the reason (services/api.js).
    // Anything else (database away, network blip) is retried by the next interaction.
  } finally {
    inFlight = false
  }
}

function onInteraction() {
  lastInteractionAt = Date.now()
  if (!signedIn()) return
  const sinceLast = lastInteractionAt - lastSentAt
  if (sinceLast >= REPORT_INTERVAL_MS) {
    send()
    return
  }
  if (pendingTimer) return
  pendingTimer = setTimeout(() => {
    pendingTimer = null
    if (lastInteractionAt > lastSentAt) send()
  }, REPORT_INTERVAL_MS - sinceLast)
}

const INTERACTION_EVENTS = ['pointerdown', 'keydown']

/**
 * Starts listening; call once from App.vue. Returns a function that stops it.
 * Route changes count as interaction, including the first page shown after a reload.
 */
export function startActivityTracker(router) {
  for (const name of INTERACTION_EVENTS) {
    document.addEventListener(name, onInteraction, { capture: true, passive: true })
  }
  const removeAfterEach = router.afterEach(() => onInteraction())
  return () => {
    for (const name of INTERACTION_EVENTS) {
      document.removeEventListener(name, onInteraction, { capture: true })
    }
    removeAfterEach()
    if (pendingTimer) {
      clearTimeout(pendingTimer)
      pendingTimer = null
    }
  }
}
