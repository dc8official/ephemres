<template>
  <!-- Shown for any address the router does not know. Before this page an unknown address
       (a mistyped link, an old bookmark such as /traffic) rendered an empty content area,
       which looked like a broken page (GNS3 LNMP II lab, 2026-11-17). -->
  <section class="not-found" aria-labelledby="not-found-title">
    <h1 id="not-found-title">Page not found</h1>
    <p>There is no page at <code>{{ route.fullPath }}</code>.</p>
    <router-link to="/" class="btn-primary">Go to the dashboard</router-link>
  </section>
</template>

<script setup>
import { useRoute } from 'vue-router'

const route = useRoute()
</script>

<style scoped>
.not-found { max-width: 520px; margin: 80px auto; padding: 32px; text-align: center; }
.not-found h1 { font-size: 22px; margin: 0 0 12px; }
.not-found p { color: var(--text-muted); margin: 0 0 24px; word-break: break-all; }
.not-found .btn-primary { display: inline-block; text-decoration: none; }
</style>
