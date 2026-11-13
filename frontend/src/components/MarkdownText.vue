<script setup lang="ts">
import { computed } from 'vue'
import MarkdownInline from './MarkdownInline.vue'
import { parseBlocks, parseInline } from '../lib/markdown'

const props = defineProps<{ text: string; inline?: boolean }>()

const blocks = computed(() => (props.inline ? [] : parseBlocks(props.text)))
const inlineTokens = computed(() => (props.inline ? parseInline(props.text.replace(/\s*\n\s*/g, ' ')) : []))

function headingTag(level: number): string {
  return `h${Math.min(level + 2, 6)}`
}
</script>

<template>
  <span v-if="inline" class="md-inline"><MarkdownInline :tokens="inlineTokens" /></span>
  <div v-else class="md-body">
    <template v-for="(block, index) in blocks" :key="index">
      <component :is="headingTag(block.level)" v-if="block.type === 'heading'" class="md-heading">
        <MarkdownInline :tokens="block.children" />
      </component>
      <hr v-else-if="block.type === 'rule'" class="md-rule" />
      <pre v-else-if="block.type === 'code'" class="md-pre"><code>{{ block.text }}</code></pre>
      <blockquote v-else-if="block.type === 'quote'" class="md-quote">
        <MarkdownInline :tokens="block.children" />
      </blockquote>
      <ol v-else-if="block.type === 'list' && block.ordered" class="md-list" :start="block.start">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex"><MarkdownInline :tokens="item" /></li>
      </ol>
      <ul v-else-if="block.type === 'list'" class="md-list">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex"><MarkdownInline :tokens="item" /></li>
      </ul>
      <p v-else class="md-paragraph"><MarkdownInline :tokens="block.children" /></p>
    </template>
  </div>
</template>
