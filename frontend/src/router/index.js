import { createRouter, createWebHistory } from 'vue-router'
import { loadUserFromStorage, isValidUserObject } from '../services/auth'

const routes = [
  {
    path: '/',
    name: 'Dashboard',
    component: () => import('../views/DashboardView.vue'),
  },
  {
    path: '/topology',
    name: 'Topology',
    component: () => import('../components/TopologyMap.vue'),
  },
  {
    path: '/reports',
    name: 'Reports',
    component: () => import('../views/ReportsView.vue'),
  },
  {
    path: '/bandwidth',
    name: 'Bandwidth',
    component: () => import('../views/BandwidthView.vue'),
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('../views/SettingsView.vue'),
  },
  // User management is a tab of the Settings page.
  {
    path: '/users',
    name: 'UserManagement',
    component: () => import('../views/SettingsView.vue'),
  },
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/LoginView.vue'),
  },
  {
    path: '/endpoints/:id',
    name: 'EndpointDetail',
    component: () => import('../views/EndpointDetailView.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})

// Pages need a signed-in user. The session cookie is HttpOnly, so the browser can't check it;
// this only looks at the profile saved at sign-in (services/auth.js). The API is the real
// check: a 401 from any call clears the profile and returns to /login (services/api.js).
router.beforeEach((to, from, next) => {
  const currentUserState = loadUserFromStorage()
  const isAuthenticated = isValidUserObject(currentUserState)

  if (to.path !== '/login' && !isAuthenticated) {
    next('/login')
  } else if (to.path === '/login' && isAuthenticated) {
    next('/')
  } else {
    next()
  }
})

export default router
