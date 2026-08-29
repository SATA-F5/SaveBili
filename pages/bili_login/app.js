import { TEMPLATE } from './template.js';
const { createApp, ref, reactive, onMounted, onUnmounted, computed } = Vue;
createApp({
    setup() {
        const bridge = window.AstrBotPluginPage;
        const activeSection = ref('qrcode'); // qrcode | password | cookie-input | cookie-manage | config
        const statusText = ref('');
        const hasCookie = ref(false);
        const isLoading = ref(false);
        const showClearConfirm = ref(false);
        const clearTargetIndex = ref(null);
        const showClearAllConfirm = ref(false);
        // Toast 通知
        const toastMessage = ref('');
        const toastVisible = ref(false);
        let toastTimer = null;
        const showToast = (msg) => {
            toastMessage.value = msg;
            toastVisible.value = true;
            if (toastTimer) clearTimeout(toastTimer);
            toastTimer = setTimeout(() => { toastVisible.value = false; }, 2500);
        };
        // 扫码
        const qrUrl = ref('');
        let pollTimer = null;
        // 账密
        const showPasswordLogin = ref(false);
        const passwordForm = reactive({ username: '', password: '' });
        const passwordLoading = ref(false);
        // Cookie 输入
        const manualCookie = reactive({ cookie: '', remark: '', priority: 10, enabled: true });
        // Cookie 列表
        const cookies = ref([]);
        // 配置
        const configForm = reactive({ download_dir: '', max_size_mb: 200, bot_qq: '', quality: 80 });
        const configLoading = ref(false);
        const apiGet = (path, params) => bridge.apiGet(path, params);
        const apiPost = (path, data) => bridge.apiPost(path, data);
        // 状态加载
        const loadStatus = async () => {
            try {
                const data = await apiGet('api/cookies');
                cookies.value = data.cookies || [];
                hasCookie.value = cookies.value.some(c => c.enabled);
            } catch (e) { console.error(e); }
        };
        // 扫码
        const generateQR = async () => {
            isLoading.value = true;
            statusText.value = '正在生成二维码...';
            try {
                const data = await apiGet('api/qrcode/generate');
                if (data.success) {
                    qrUrl.value = data.image_data_url;
                    statusText.value = '请使用 B 站 App 扫码';
                    startPolling();
                } else {
                    statusText.value = '生成二维码失败：' + (data.error || '未知错误');
                }
            } catch (e) {
                statusText.value = '请求异常：' + e.message;
            } finally {
                isLoading.value = false;
            }
        };
        const startPolling = () => {
            if (pollTimer) clearInterval(pollTimer);
            pollTimer = setInterval(async () => {
                try {
                    const data = await apiGet('api/cookies');
                    cookies.value = data.cookies || [];
                    const hasAny = cookies.value.some(c => c.enabled);
                    if (hasAny) {
                        hasCookie.value = true;
                        statusText.value = '✅ 登录成功！Cookie 已保存';
                        showToast('登录成功');
                        clearInterval(pollTimer);
                        pollTimer = null;
                    }
                } catch (e) { console.error(e); }
            }, 3000);
        };
        // 账密
        const submitPasswordLogin = async () => {
            if (!passwordForm.username || !passwordForm.password) {
                statusText.value = '请输入用户名和密码';
                return;
            }
            passwordLoading.value = true;
            statusText.value = '正在登录...';
            try {
                const data = await apiPost('api/login/password', passwordForm);
                if (data.success) {
                    statusText.value = '✅ 登录成功！Cookie 已保存';
                    showToast('登录成功');
                    await loadStatus();
                } else {
                    statusText.value = '登录失败：' + (data.error || '未知错误');
                }
            } catch (e) {
                statusText.value = '请求异常：' + e.message;
            } finally {
                passwordLoading.value = false;
            }
        };
        // 手动添加 Cookie
        const addManualCookie = async () => {
            if (!manualCookie.cookie) {
                statusText.value = 'Cookie 不能为空';
                return;
            }
            try {
                const data = await apiPost('api/cookies/add', {
                    cookie: manualCookie.cookie,
                    remark: manualCookie.remark,
                    priority: manualCookie.priority,
                    enabled: manualCookie.enabled,
                });
                if (data.success) {
                    statusText.value = 'Cookie 添加成功';
                    showToast('Cookie 添加成功');
                    manualCookie.cookie = '';
                    manualCookie.remark = '';
                    manualCookie.priority = 10;
                    manualCookie.enabled = true;
                    await loadStatus();
                } else {
                    statusText.value = '添加失败：' + (data.error || '未知错误');
                }
            } catch (e) {
                statusText.value = '请求异常：' + e.message;
            }
        };
        // Cookie 管理
        const toggleCookie = async (index) => {
            const c = cookies.value[index];
            const data = await apiPost('api/cookies/update', {
                index,
                enabled: !c.enabled,
            });
            if (data.success) {
                await loadStatus();
                showToast(c.enabled ? '已禁用该 Cookie' : '已启用该 Cookie');
            }
        };
        const updatePriority = async (index, priority) => {
            const data = await apiPost('api/cookies/update', {
                index,
                priority: parseInt(priority) || 100,
            });
            if (data.success) {
                await loadStatus();
                showToast('优先级已更新');
            }
        };
        const requestDeleteCookie = (index) => {
            clearTargetIndex.value = index;
            showClearConfirm.value = true;
        };
        const confirmDeleteCookie = async () => {
            showClearConfirm.value = false;
            if (clearTargetIndex.value === null) return;
            const data = await apiPost('api/cookies/delete', { index: clearTargetIndex.value });
            if (data.success) {
                statusText.value = 'Cookie 已删除';
                showToast('Cookie 已删除');
                clearTargetIndex.value = null;
                await loadStatus();
            }
        };
        const cancelDeleteCookie = () => {
            showClearConfirm.value = false;
            clearTargetIndex.value = null;
        };
        const requestClearAll = () => {
            showClearAllConfirm.value = true;
        };
        const confirmClearAll = async () => {
            showClearAllConfirm.value = false;
            const data = await apiPost('api/cookies/clear', {});
            if (data.success) {
                statusText.value = '所有 Cookie 已清除';
                showToast('所有 Cookie 已清除');
                await loadStatus();
            }
        };
        const cancelClearAll = () => {
            showClearAllConfirm.value = false;
        };
        // 配置
        const loadConfig = async () => {
            const data = await apiGet('api/config');
            if (data.success) {
                configForm.download_dir = data.config.download_dir;
                configForm.max_size_mb = data.config.max_size_mb;
                configForm.bot_qq = data.config.bot_qq;
                configForm.quality = data.config.quality;
            }
        };
        const saveConfig = async () => {
            configLoading.value = true;
            try {
                const data = await apiPost('api/config/update', {
                    download_dir: configForm.download_dir,
                    max_size_mb: configForm.max_size_mb,
                    bot_qq: configForm.bot_qq,
                    quality: configForm.quality,
                });
                if (data.success) {
                    statusText.value = '配置已保存';
                    showToast('配置已保存');
                } else {
                    statusText.value = '保存失败：' + (data.error || '未知错误');
                }
            } catch (e) {
                statusText.value = '请求异常：' + e.message;
            } finally {
                configLoading.value = false;
            }
        };
        onMounted(async () => {
            await loadStatus();
            await loadConfig();
            if (cookies.value.length === 0) {
                activeSection.value = 'qrcode';
                await generateQR();
            }
        });
        onUnmounted(() => {
            if (pollTimer) clearInterval(pollTimer);
            if (toastTimer) clearTimeout(toastTimer);
        });
        return {
            activeSection,
            statusText,
            qrUrl,
            isLoading,
            showPasswordLogin,
            passwordForm,
            passwordLoading,
            manualCookie,
            cookies,
            configForm,
            configLoading,
            showClearConfirm,
            showClearAllConfirm,
            toastMessage,
            toastVisible,
            generateQR,
            submitPasswordLogin,
            addManualCookie,
            toggleCookie,
            updatePriority,
            requestDeleteCookie,
            confirmDeleteCookie,
            cancelDeleteCookie,
            requestClearAll,
            confirmClearAll,
            cancelClearAll,
            saveConfig,
        };
    },
    template: TEMPLATE,
}).mount('#app');
