import { ref } from 'vue'

const sseConnected = ref(false)
let eventSource = null
let wasDisconnected = false
const listeners = new Set()
const reconnectCallbacks = new Set()
let reconnectTimer = null
let retryAttempt = 0
let retryTimer = null

function isUserLoggedIn() {
  if (typeof window === 'undefined') return false
  try {
    return !!sessionStorage.getItem('user') || !!localStorage.getItem('user')
  } catch (e) {
    return false
  }
}

function triggerReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer)
  reconnectTimer = setTimeout(() => {
    reconnectCallbacks.forEach((cb) => {
      try {
        cb()
      } catch (err) {
        console.error('Error in SSE reconnect callback:', err)
      }
    })
  }, 300)
}

function scheduleRetry() {
  if (retryTimer) clearTimeout(retryTimer)
  const delay = Math.min(1000 * Math.pow(2, retryAttempt), 15000)
  retryAttempt++
  retryTimer = setTimeout(() => {
    if (isUserLoggedIn() && (!eventSource || eventSource.readyState === EventSource.CLOSED)) {
      ensureConnection()
    }
  }, delay)
}

export function closeConnection() {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
  sseConnected.value = false
  wasDisconnected = false
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (retryTimer) {
    clearTimeout(retryTimer)
    retryTimer = null
  }
  retryAttempt = 0
}

export function resetConnection() {
  closeConnection()
  ensureConnection()
}

function ensureConnection() {
  if (typeof window === 'undefined') return
  if (!isUserLoggedIn()) return

  if (!eventSource || eventSource.readyState === EventSource.CLOSED) {
    try {
      eventSource = new EventSource('/api/v1/events/stream', { withCredentials: true })
      eventSource.onopen = () => {
        sseConnected.value = true
        retryAttempt = 0
        if (retryTimer) {
          clearTimeout(retryTimer)
          retryTimer = null
        }
        if (wasDisconnected) {
          wasDisconnected = false
          triggerReconnect()
        }
      }
      eventSource.onmessage = (event) => {
        if (!event.data) return
        listeners.forEach((callback) => {
          try {
            callback(event)
          } catch (err) {
            console.error('Error in SSE listener:', err)
          }
        })
      }
      eventSource.onerror = () => {
        sseConnected.value = false
        wasDisconnected = true
        if (eventSource.readyState === EventSource.CLOSED) {
          scheduleRetry()
        }
      }
    } catch (err) {
      sseConnected.value = false
      wasDisconnected = true
      scheduleRetry()
    }
  }
}

export function useSSE() {
  ensureConnection()

  function subscribe(callback) {
    ensureConnection()
    listeners.add(callback)
    return () => {
      listeners.delete(callback)
    }
  }

  function subscribeBatched(batchCallback) {
    let queue = []
    let rafId = null

    const flush = () => {
      rafId = null
      if (queue.length === 0) return
      const batch = queue
      queue = []
      try {
        batchCallback(batch)
      } catch (err) {
        console.error('Error in batched SSE listener:', err)
      }
    }

    const unsub = subscribe((event) => {
      if (!event?.data) return
      try {
        const parsed = JSON.parse(event.data)
        queue.push(parsed)
        if (rafId === null) {
          rafId = typeof requestAnimationFrame !== 'undefined'
            ? requestAnimationFrame(flush)
            : setTimeout(flush, 16)
        }
      } catch (e) {
        // Ignore non-JSON heartbeat comments
      }
    })

    return () => {
      unsub()
      if (rafId !== null) {
        if (typeof cancelAnimationFrame !== 'undefined') cancelAnimationFrame(rafId)
        else clearTimeout(rafId)
        rafId = null
      }
      queue = []
    }
  }

  function onReconnect(callback) {
    reconnectCallbacks.add(callback)
    return () => {
      reconnectCallbacks.delete(callback)
    }
  }

  return {
    sseConnected,
    subscribe,
    subscribeBatched,
    onReconnect,
    closeConnection,
    resetConnection,
  }
}

