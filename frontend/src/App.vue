<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()
const workspaces = [
  { to: '/', label: '题库', matches: (path: string) => path === '/' || path === '/questions' },
  { to: '/materials', label: '简历', matches: (path: string) => path === '/materials' },
  { to: '/planner', label: '计划', matches: (path: string) => path === '/planner' },
  { to: '/interviews', label: '练习', matches: (path: string) => path.startsWith('/interviews') },
]
</script>

<template>
  <div class="studio-shell">
    <header class="topbar">
      <RouterLink class="brand" to="/interviews" aria-label="练习场首页">
        <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
        <span>练习场<span class="brand-dot">.</span></span>
      </RouterLink>
      <nav class="topnav" aria-label="主导航">
        <RouterLink v-for="item in workspaces" :key="item.to" :to="item.to"
          class="nav-link" :class="{ 'nav-link-active': item.matches(route.path) }"
          :aria-current="item.matches(route.path) ? 'page' : undefined">
          {{ item.label }}
        </RouterLink>
      </nav>
      <div class="topbar-meta"><span class="system-dot" aria-hidden="true"></span><span>本地工作区</span></div>
    </header>
    <RouterView />
  </div>
</template>
