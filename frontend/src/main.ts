import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import './styles.css'
import {
  ChatDotRound,
  ChatLineRound,
  Clock,
  Collection,
  DataBoard,
  Document,
  List,
  OfficeBuilding,
  ShoppingCart,
  User,
} from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'

const app = createApp(App)
for (const component of [
  ChatDotRound, ChatLineRound, Clock, Collection, DataBoard,
  Document, List, OfficeBuilding, ShoppingCart, User,
]) app.component(component.name, component)
app.use(createPinia())
app.use(router)
app.use(ElementPlus)
app.mount('#app')
