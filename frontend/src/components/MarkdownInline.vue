<script setup lang="ts">
import type { InlineNode } from '../lib/markdown'

defineProps<{ tokens: InlineNode[] }>()
</script>

<template>
  <template v-for="(token, index) in tokens" :key="index">
    <strong v-if="token.type === 'strong'"><MarkdownInline :tokens="token.children" /></strong>
    <em v-else-if="token.type === 'emphasis'"><MarkdownInline :tokens="token.children" /></em>
    <s v-else-if="token.type === 'strike'"><MarkdownInline :tokens="token.children" /></s>
    <code v-else-if="token.type === 'code'" class="md-code">{{ token.text }}</code>
    <a v-else-if="token.type === 'link'" class="md-link" :href="token.href" target="_blank" rel="noreferrer noopener"><MarkdownInline :tokens="token.children" /></a>
    <br v-else-if="token.type === 'break'" />
    <template v-else>{{ token.text }}</template>
  </template>
</template>
