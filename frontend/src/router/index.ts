import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', name: 'Login', component: () => import('../views/LoginView.vue') },
  {
    path: '/',
    component: () => import('../components/layout/AppLayout.vue'),
    children: [
      { path: '', redirect: '/dashboard' },
      { path: 'dashboard', name: 'Dashboard', component: () => import('../views/DashboardView.vue') },
      { path: 'qa', name: 'QA', component: () => import('../views/QAChatView.vue') },
      { path: 'bid-review', name: 'BidReview', component: () => import('../views/BidReviewView.vue') },
      { path: 'bim', name: 'BimReview', component: () => import('../views/BimReviewView.vue') },
      { path: 'procurement', name: 'Procurement', component: () => import('../views/ProcurementView.vue') },
      { path: 'negotiation', name: 'Negotiation', component: () => import('../views/NegotiationView.vue') },
      { path: 'teacher', name: 'Teacher', component: () => import('../views/TeacherView.vue') },
      { path: 'history', name: 'History', component: () => import('../views/HistoryView.vue') },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const token = localStorage.getItem('bm_token')
  if (to.path !== '/login' && !token) return '/login'
  if (to.path === '/login' && token) return '/dashboard'
  return true
})

export default router
