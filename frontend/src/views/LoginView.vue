<template>
  <div class="login-wrapper">
    <div class="login-container">
      <div class="login-card">
        <div class="brand-header">
          <h1 class="brand-title">Ephemres v3.3.2.v</h1>
          <p class="brand-subtitle">Network Uptime Monitoring Platform</p>
        </div>

        <form 
          @submit.prevent="handleLogin" 
          action="/api/v1/auth/login" 
          method="post" 
          autocomplete="on" 
          class="login-form"
        >
          <div v-if="error" class="error-container" role="alert">
            <Message severity="error" :closable="false">{{ error }}</Message>
          </div>

          <div class="form-group">
            <label for="username">Username</label>
            <div class="input-with-icon">
              <i class="pi pi-user field-icon" aria-hidden="true"></i>
              <input 
                id="username" 
                name="username"
                type="text"
                autocomplete="username"
                autocorrect="off"
                autocapitalize="off"
                spellcheck="false"
                v-model="username" 
                placeholder="Enter your username" 
                required 
                class="login-input"
                :disabled="loading"
              />
            </div>
          </div>

          <div class="form-group">
            <label for="password">Password</label>
            <div class="input-with-icon password-wrapper">
              <i class="pi pi-lock field-icon" aria-hidden="true"></i>
              <input 
                id="password" 
                name="password"
                :type="showPassword ? 'text' : 'password'"
                autocomplete="current-password"
                v-model="password" 
                placeholder="Enter your password" 
                required 
                class="login-input password-input"
                :disabled="loading"
              />
              <i 
                class="pi toggle-icon"
                :class="showPassword ? 'pi-eye-slash' : 'pi-eye'"
                @click="showPassword = !showPassword"
                title="Toggle password visibility"
                tabindex="0"
                @keydown.enter="showPassword = !showPassword"
                role="button"
                aria-label="Toggle password visibility"
              ></i>
            </div>
          </div>

          <button 
            type="submit" 
            name="login-submit"
            class="submit-button" 
            :disabled="loading"
          >
            <i v-if="loading" class="pi pi-spin pi-spinner" style="margin-right: 0.5rem;"></i>
            <i v-else class="pi pi-sign-in" style="margin-right: 0.5rem;"></i>
            <span>{{ loading ? 'Signing In...' : 'Sign In' }}</span>
          </button>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { login } from '../services/api.js'
import { setUserState } from '../services/auth.js'
import Message from 'primevue/message'

const router = useRouter()
const username = ref('')
const password = ref('')
const showPassword = ref(false)
const loading = ref(false)
const error = ref(null)

const handleLogin = async () => {
  const u = (username.value || '').trim()
  const p = password.value || ''

  if (!u || !p) {
    error.value = 'Please enter both username and password.'
    return
  }

  loading.value = true
  error.value = null

  try {
    const response = await login(u, p)
    const payloadData = response.data?.data || response.data
    setUserState({
      username: payloadData.username,
      role: payloadData.role,
      must_change_password: payloadData.must_change_password
    })
    router.push('/')
  } catch (err) {
    if (err.response) {
      const status = err.response.status
      const detail = err.response.data?.detail
      if (status === 403) {
        error.value = detail || 'Account temporarily locked for 15 minutes due to multiple failed login attempts.'
      } else if (status === 401) {
        error.value = detail || 'Invalid username or password. Please verify your credentials.'
      } else if (status === 429) {
        error.value = 'Too many requests. Please wait a moment before trying again.'
      } else if (status >= 500) {
        error.value = 'Server connection error. Please ensure the Ephemres backend service is running.'
      } else {
        error.value = detail || 'Authentication failed. Please check your credentials and try again.'
      }
    } else {
      error.value = 'Unable to connect to Ephemres server. Please check your network connection.'
    }
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrapper {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100vh;
  background-color: var(--bg-app);
  background-image: 
    radial-gradient(at 0% 0%, rgba(128, 128, 128, 0.05) 0px, transparent 50%),
    radial-gradient(at 100% 100%, rgba(128, 128, 128, 0.03) 0px, transparent 50%);
  padding: 1.5rem;
  transition: background-color 0.2s ease;
}

.login-container {
  width: 100%;
  max-width: 420px;
}

.login-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius, 8px);
  box-shadow: var(--shadow-hover);
  padding: 2rem;
  transition: background-color 0.2s ease, border-color 0.2s ease;
}

.brand-header {
  text-align: center;
  margin-bottom: 2rem;
}

.brand-title {
  font-family: var(--font-display);
  font-size: 1.75rem;
  font-weight: 800;
  color: var(--text-primary);
  letter-spacing: 0.02em;
  margin: 0;
  text-transform: lowercase;
}

.brand-subtitle {
  font-family: var(--font-display);
  font-size: 0.8125rem;
  color: var(--text-secondary);
  margin-top: 0.35rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: 600;
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}

.error-container {
  margin-bottom: 0.5rem;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.form-group label {
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--text-secondary);
  letter-spacing: -0.01em;
}

.input-with-icon {
  position: relative;
  display: flex;
  align-items: center;
}

.field-icon {
  position: absolute;
  left: 0.875rem;
  color: var(--text-muted);
  pointer-events: none;
  font-size: 0.9375rem;
  z-index: 1;
}

.login-input {
  padding-left: 2.5rem;
  height: 2.625rem;
  background: var(--bg-surface-hover);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  border-radius: var(--radius-sm, 6px);
  font-size: 0.875rem;
  font-family: var(--font-sans);
  transition: all 0.15s ease;
  width: 100%;
}

.login-input:focus {
  outline: none;
  border-color: var(--text-primary);
  box-shadow: 0 0 0 1px var(--text-primary);
  background: var(--bg-surface);
}

.password-wrapper .login-input {
  padding-right: 2.5rem;
}

.toggle-icon {
  position: absolute;
  right: 0.875rem;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 0.9375rem;
  padding: 0.25rem;
  transition: color 0.15s ease;
}

.toggle-icon:hover {
  color: var(--text-primary);
}

.submit-button {
  height: 2.625rem;
  margin-top: 0.5rem;
  background: var(--accent);
  color: var(--text-inverse);
  border: none;
  font-weight: 600;
  font-size: 0.875rem;
  font-family: var(--font-sans);
  border-radius: var(--radius-sm, 6px);
  cursor: pointer;
  display: flex;
  justify-content: center;
  align-items: center;
  transition: opacity 0.15s ease, transform 0.15s ease;
}

.submit-button:hover:not(:disabled) {
  opacity: 0.9;
  transform: translateY(-1px);
}

.submit-button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (max-width: 480px) {
  .login-container {
    max-width: 100%;
  }
}
</style>
