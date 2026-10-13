import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHashHistory } from 'vue-router'
import { createVuetify } from 'vuetify'
import {
  VApp, VAppBar, VBtn, VCard, VCardActions, VCardText, VCardTitle, VCheckbox,
  VDialog, VExpansionPanel, VExpansionPanels, VExpansionPanelText, VForm,
  VMain, VNavigationDrawer, VProgressLinear, VSelect, VSnackbar, VSpacer,
  VSwitch, VTextarea, VTextField, VMenu, VList, VListItem,
} from 'vuetify/components'
import { Ripple } from 'vuetify/directives'
import { Square, SquareCheckBig, SquareMinus } from 'lucide-vue-next'
import 'vuetify/styles'
import './style.css'
import App from './App.vue'
import Workspace from './Workspace.vue'
import AdminViews from './AdminViews.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/tasks' },
    { path: '/overview', redirect: '/tasks' },
    { path: '/documents', redirect: '/offline' },
    { path: '/connections', redirect: '/settings/connections' },
    { path: '/settings/connections', component: AdminViews, meta: { view: 'connections' } },
    { path: '/:view(rules|security|logs)', component: AdminViews },
    { path: '/schedules', redirect: '/settings' },
    { path: '/audit', redirect: '/logs' },
    { path: '/:view?', component: Workspace },
  ],
})

const vuetify = createVuetify({
  components: {
    VApp, VAppBar, VBtn, VCard, VCardActions, VCardText, VCardTitle, VCheckbox,
    VDialog, VExpansionPanel, VExpansionPanels, VExpansionPanelText, VForm,
    VMain, VNavigationDrawer, VProgressLinear, VSelect, VSnackbar, VSpacer,
    VSwitch, VTextarea, VTextField, VMenu, VList, VListItem,
  },
  directives: { Ripple },
  theme: { defaultTheme: 'light', themes: {
    light: { colors: {
      primary: '#397fbd', secondary: '#52616b', background: '#f5f7f8',
      surface: '#ffffff', error: '#be3543', success: '#21845a', warning: '#ad6a08',
    } },
  } },
  defaults: {
    VBtn: { variant: 'flat', rounded: 'sm', elevation: 0 },
    VTextField: { variant: 'outlined', density: 'compact', hideDetails: 'auto' },
    VSelect: { variant: 'outlined', density: 'compact', hideDetails: 'auto' },
    VTextarea: { variant: 'outlined', hideDetails: 'auto' },
    VCheckbox: { trueIcon: SquareCheckBig, falseIcon: Square, indeterminateIcon: SquareMinus },
  },
})

createApp(App).use(createPinia()).use(router).use(vuetify).mount('#app')
