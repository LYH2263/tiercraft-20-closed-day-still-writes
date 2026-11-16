<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const domains = ref([])
const domainId = ref(1)
const rows = ref([])
const err = ref('')
const busy = ref(false)
const loadDay = ref('')
const lastJob = ref(null)
const lastDecisions = ref([])

async function refresh() {
  rows.value = await getJson(`/api/layers/贴源?domain_id=${domainId.value}`)
}

onMounted(async () => {
  try {
    domains.value = await getJson('/api/domains')
    if (domains.value.length) domainId.value = domains.value[0].id
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function reload() {
  busy.value = true
  err.value = ''
  try {
    lastJob.value = await postJson('/api/paste/load', {
      domain_id: domainId.value,
      biz_day: loadDay.value || null,
    })
    lastDecisions.value = await getJson(
      `/api/close-decisions?load_id=${lastJob.value.id}`,
    )
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <div>
    <h2>贴源层</h2>
    <p class="lead">尽量保持来源形态入仓，供后续明细清洗使用。已关闭业务日的贴源禁止再写入，装载时跳过并在任务结果中注明。</p>
    <div class="toolbar">
      <label>
        主题域
        <select v-model.number="domainId" @change="refresh">
          <option v-for="d in domains" :key="d.id" :value="d.id">{{ d.name }}</option>
        </select>
      </label>
      <label>指定业务日装载（留空为全部业务日）<input v-model="loadDay" type="date" /></label>
      <button :disabled="busy" @click="reload">重新装载贴源夹具</button>
    </div>
    <p v-if="err" class="err">{{ err }}</p>
    <div v-if="lastJob" class="panel">
      <p>
        最近装载：<span :class="lastJob.reject_count ? 'warn' : 'ok'">{{ lastJob.status }}</span>
        ，输入 {{ lastJob.in_count }} 行，写入 {{ lastJob.out_count }} 行，拒绝 {{ lastJob.reject_count }} 行
        <template v-if="lastJob.reason_code">，拒绝原因码 <span class="warn">{{ lastJob.reason_code }}</span></template>
      </p>
      <p class="muted">{{ lastJob.message }}</p>
      <h4 v-if="lastDecisions.length">本次装载关闭裁决证明（按装载编号 {{ lastJob.id }} 只读）</h4>
      <table v-if="lastDecisions.length">
        <thead>
          <tr><th>目标日</th><th>是否关闭</th><th>写路径是否放行</th><th>原因码</th></tr>
        </thead>
        <tbody>
          <tr v-for="d in lastDecisions" :key="d.id" :class="d.write_allowed ? '' : 'warn'">
            <td>{{ d.biz_day }}</td>
            <td>
              <span v-if="d.is_closed" class="badge badge-closed">已关闭</span>
              <span v-else class="badge badge-open">未关闭</span>
            </td>
            <td>
              <span v-if="d.write_allowed" class="badge badge-open">放行</span>
              <span v-else class="badge badge-closed">拒绝</span>
            </td>
            <td>{{ d.reason_code || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>编号</th><th>主题域</th><th>业务日</th><th>状态</th><th>门店编码</th><th>门店名称</th>
            <th>品类</th><th>金额</th><th>件数</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.domain_id }}</td>
            <td>{{ r.biz_day }}</td>
            <td>
              <span v-if="r.closed" class="badge badge-closed">已关闭</span>
              <span v-else class="badge badge-open">未关闭</span>
            </td>
            <td>{{ r.store_code }}</td>
            <td>{{ r.store_name || '（空）' }}</td>
            <td>{{ r.category }}</td>
            <td>{{ r.amount }}</td>
            <td>{{ r.qty }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!rows.length" class="muted">暂无贴源数据。</p>
    </div>
  </div>
</template>
