// Who is signed in, as far as the UI is concerned. The session itself is an HttpOnly cookie the
// browser can't read, so sign-in also stores the username and role in sessionStorage (gone when
// the tab closes) to decide what to show. It is not a security boundary: the server checks the
// cookie, the role and any forced password change on every request.
import { ref } from 'vue'
import { closeConnection, resetConnection } from '../composables/useSSE.js'

export const user = ref(null)
export const currentUser = ref(null)
export const isAdmin = ref(false)
export const isOperatorOrAdmin = ref(false)
export const mustChangePassword = ref(false)

export function isValidUserObject(parsed) {
  return (
    parsed !== null &&
    typeof parsed === 'object' &&
    typeof parsed.username === 'string' &&
    parsed.username.trim().length > 0 &&
    typeof parsed.role === 'string' &&
    parsed.role.trim().length > 0
  )
}

export function loadUserFromStorage() {
  // Use sessionStorage to prevent cleartext credentials/tokens persisting in localStorage (CWE-312 / CWE-315)
  let storedUser = null
  try {
    storedUser = sessionStorage.getItem('user')
    // Fallback and migrate legacy localStorage entry if present
    if (!storedUser && typeof localStorage !== 'undefined') {
      storedUser = localStorage.getItem('user')
      if (storedUser) {
        localStorage.removeItem('user')
        sessionStorage.setItem('user', storedUser)
      }
    }
  } catch (e) {
    console.error('Failed to access session storage:', e)
  }

  if (storedUser) {
    try {
      const parsed = JSON.parse(storedUser)
      if (isValidUserObject(parsed)) {
        const sanitized = {
          username: parsed.username,
          role: parsed.role,
          must_change_password: !!(parsed.requires_setup ?? parsed.must_change_password)
        }
        user.value = sanitized
        currentUser.value = sanitized.username
        isAdmin.value = sanitized.role === 'ADMIN'
        isOperatorOrAdmin.value = ['ADMIN', 'OPERATOR'].includes(sanitized.role)
        mustChangePassword.value = sanitized.must_change_password
        return sanitized
      }
    } catch (e) {
      console.error('Failed to parse user state:', e)
    }
  }
  clearUserState()
  return null
}

export function setUserState(userData) {
  if (!userData) {
    clearUserState()
    return
  }

  // Store a sanitized profile in sessionStorage without cleartext sensitive auth payload (CWE-312)
  const sanitizedProfile = {
    username: userData.username,
    role: userData.role,
    requires_setup: !!userData.must_change_password
  }

  try {
    sessionStorage.setItem('user', JSON.stringify(sanitizedProfile))
    if (typeof localStorage !== 'undefined') {
      localStorage.removeItem('user')
    }
  } catch (e) {
    console.error('Failed to write to session storage:', e)
  }

  user.value = {
    username: userData.username,
    role: userData.role,
    must_change_password: !!userData.must_change_password
  }
  currentUser.value = userData.username || null
  isAdmin.value = userData.role === 'ADMIN'
  isOperatorOrAdmin.value = ['ADMIN', 'OPERATOR'].includes(userData.role)
  mustChangePassword.value = !!userData.must_change_password

  try {
    resetConnection()
  } catch (e) {}
}

export function clearUserState() {
  try {
    sessionStorage.removeItem('user')
    if (typeof localStorage !== 'undefined') {
      localStorage.removeItem('user')
    }
  } catch (e) {
    console.error('Failed to clear session storage:', e)
  }

  user.value = null
  currentUser.value = null
  isAdmin.value = false
  isOperatorOrAdmin.value = false
  mustChangePassword.value = false

  try {
    closeConnection()
  } catch (e) {}
}
