<template>
  <el-card>
    <template #header>📜 历史记录</template>
    <el-tabs v-model="tab" @tab-change="load">
      <el-tab-pane label="投标审查记录" name="bid">
        <el-table :data="items" v-loading="loading">
          <el-table-column prop="doc_name" label="文档" />
          <el-table-column prop="status" label="状态" />
          <el-table-column prop="created_at" label="时间" />
          <el-table-column label="操作" width="120">
            <template #default="s">
              <el-button size="small" type="primary" link @click="showBidDetail(s.row.id)">查看报告</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
      <el-tab-pane label="我的采购单" name="proc">
        <el-table :data="items" v-loading="loading">
          <el-table-column prop="order_no" label="单号" />
          <el-table-column prop="material_name" label="材料" />
          <el-table-column label="金额"><template #default="s">{{ s.row.total_amount }} 元</template></el-table-column>
          <el-table-column prop="status" label="状态" />
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 投标报告详情弹窗 -->
    <el-dialog v-model="detailVisible" title="投标审查报告" width="640">
      <pre v-if="detailText" style="white-space:pre-wrap; max-height:60vh; overflow:auto">{{ detailText }}</pre>
    </el-dialog>
  </el-card>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { bidList, bidPoll, myOrders } from '../api'
import { apiErrorMessage } from '../utils/apiError'
import { formatBidReport } from '../utils/bidReport'
const tab = ref('bid')
const items = ref<any[]>([])
const loading = ref(false)
const detailVisible = ref(false)
const detailText = ref('')
async function load() {
  loading.value = true
  try {
    const api = tab.value === 'bid' ? bidList : myOrders
    items.value = (await api()).data.items || []
  } catch (e) {} finally { loading.value = false }
}
async function showBidDetail(id: string) {
  detailText.value = '⏳ 加载中...'
  detailVisible.value = true
  try {
    const pj = (await bidPoll(id)).data
    detailText.value = formatBidReport(pj)
  } catch (e: any) {
    detailText.value = '❌ 加载失败：' + apiErrorMessage(e, '未知错误')
  }
}
load()
</script>
