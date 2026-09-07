import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const routes = [
  { path: '/login', name: 'Login', component: () => import('../views/LoginView.vue') },
  {
    path: '/',
    component: () => import('../components/layout/AppLayout.vue'),
    children: [
      { path: '', redirect: '/dashboard' },
      { path: 'dashboard', name: 'Dashboard', component: () => import('../views/DashboardView.vue') },
      { path: 'knowledge', name: 'Knowledge', component: () => import('../views/KnowledgeView.vue'), meta: { roles: ['admin'] } },
      { path: 'tasks', name: 'Tasks', component: () => import('../views/TaskTimelineView.vue'), meta: { roles: ['admin'] } },
      { path: 'qa', name: 'QA', component: () => import('../views/QAChatView.vue') },
      // Legacy deep links remain safe, but all non-BIM business actions start in QA.
      { path: 'bid-review', redirect: { path: '/qa', query: { capability: 'bid_review' } } },
      { path: 'bim', name: 'BimReview', component: () => import('../views/BimReviewView.vue') },
      { path: 'procurement', redirect: { path: '/qa', query: { capability: 'procurement' } } },
      { path: 'negotiation', redirect: { path: '/qa', query: { capability: 'negotiation' } } },
      { path: 'review', name: 'Review', component: () => import('../views/ReviewView.vue'), meta: { roles: ['admin', 'reviewer'] } },
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
  const roles = to.meta.roles as string[] | undefined
  const role = useAuthStore().user?.role
  if (roles && (!role || !roles.includes(role))) return '/dashboard'
  return true
})

export default router
