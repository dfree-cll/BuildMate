<template>
  <el-card>
    <template #header>📜 历史记录</template>
    <el-tabs v-model="tab" @tab-change="load">
      <el-tab-pane label="投标审查记录" name="bid">
        <el-table :data="items" v-loading="loading">
          <el-table-column prop="doc_name" label="文档" />
          <el-table-column prop="status" label="状态" />
          <el-table-column prop="created_at" label="时间" />
        </el-table>
      </el-tab-pane>
      <el-tab-pane label="BIM 审查记录" name="bim">
        <el-table :data="items" v-loading="loading">
          <el-table-column prop="file_name" label="模型文件" />
          <el-table-column prop="status" label="状态" />
          <el-table-column prop="created_at" label="时间" />
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
  </el-card>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { bidList, myOrders, bimList } from '../api'
const tab = ref('bid')
const items = ref<any[]>([])
const loading = ref(false)
async function load() {
  loading.value = true
  try {
    const api = tab.value === 'bid' ? bidList : (tab.value === 'bim' ? bimList : myOrders)
    items.value = (await api()).data.items || []
  } catch (e) {} finally { loading.value = false }
}
load()
</script>
