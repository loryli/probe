import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import InterviewWorkspace from './views/InterviewWorkspace.vue'
import QuestionBankView from './views/QuestionBankView.vue'
import MaterialsView from './views/MaterialsView.vue'
import PlannerView from './views/PlannerView.vue'
import './styles/base.css'
import './styles/workspace.css'
import './styles/question-bank.css'
import './styles/materials.css'
import './styles/planner.css'

const productionBase = import.meta.env.PROD ? '/app/' : '/'
const router = createRouter({
  history: createWebHistory(productionBase),
  routes: [
    { path: '/', component: QuestionBankView },
    { path: '/questions', component: QuestionBankView },
    { path: '/materials', component: MaterialsView },
    { path: '/planner', component: PlannerView },
    { path: '/interviews/:sessionId?', component: InterviewWorkspace },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

createApp(App).use(router).mount('#app')
