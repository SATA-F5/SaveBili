import { TEMPLATE } from './template.js';
const { createApp, ref, reactive, onMounted, onUnmounted, computed } = Vue;

const TYPE_LABELS = {
    video: '视频',
    bangumi: '番剧',
    article: '专栏',
    opus: '动态',
    favlist: '收藏夹',
    collection: '合集',
};

const emptyStats = () => ({
    totals: { count: 0, success: 0, failed: 0, bytes: 0 },
    today: { count: 0, success: 0, failed: 0, bytes: 0 },
    days: [],
    types: {},
    recent: [],
    success_rate: 0,
    storage: { files: 0, bytes: 0 },
    cookies: { enabled: 0, total: 0 },
});

createApp({
    setup() {
        const bridge = window.AstrBotPluginPage;
        const activeSection = ref('dashboard'); // dashboard | qrcode | cookie-input | cookie-manage | config
        const statusText = ref('数据每 30 秒自动刷新');
        const hasCookie = ref(false);
        const isLoading = ref(false);
        const showClearConfirm = ref(false);
        const clearTargetIndex = ref(null);
        const showClearAllConfirm = ref(false);

        // 数据概览
        const stats = ref(emptyStats());
        const statsLoading = ref(false);

        // FFmpeg 状态
        const ffmpegStatus = ref({ status: 'idle', message: '尚未检测 FFmpeg' });

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
        let statsTimer = null;
        // Cookie 输入
        const manualCookie = reactive({ cookie: '', remark: '', priority: 10, enabled: true });
        // Cookie 列表
        const cookies = ref([]);
        // 配置
        const configForm = reactive({ download_dir: '', max_size_mb: 200, quality: 80 });
        const configLoading = ref(false);
        const apiGet = (path, params) => bridge.apiGet(path, params);
        const apiPost = (path, data) => bridge.apiPost(path, data);

        // ---------- 格式化 & 图表 ----------
        const formatBytes = (bytes) => {
            const n = Number(bytes) || 0;
            if (n < 1024) return n + ' B';
            const units = ['KB', 'MB', 'GB', 'TB'];
            let value = n;
            let idx = -1;
            do {
                value /= 1024;
                idx += 1;
            } while (value >= 1024 && idx < units.length - 1);
            return value.toFixed(value >= 10 ? 0 : 1) + ' ' + units[idx];
        };
        const formatTime = (ts) => {
            if (!ts) return '-';
            const d = new Date(ts * 1000);
            const pad = (x) => String(x).padStart(2, '0');
            return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
        };
        const typeLabel = (key) => TYPE_LABELS[key] || key || '其他';
        const typeRows = computed(() => {
            const entries = Object.entries(stats.value.types || {})
                .filter(([, count]) => Number(count) > 0)
                .sort((a, b) => b[1] - a[1]);
            const max = entries.length ? entries[0][1] : 1;
            return entries.map(([key, count]) => ({
                key,
                label: typeLabel(key),
                count,
                pct: Math.max(6, Math.round((count / max) * 100)),
            }));
        });
        const maxDayCount = computed(() => {
            const counts = (stats.value.days || []).map((d) => Number(d.count) || 0);
            return Math.max(1, ...counts);
        });
        const barHeight = (count) => {
            const c = Number(count) || 0;
            if (c === 0) return '3%';
            return Math.max(8, Math.round((c / maxDayCount.value) * 100)) + '%';
        };

        // ---------- 数据概览 ----------
        const loadStats = async () => {
            try {
                const data = await apiGet('api/stats');
                if (data && data.success && data.stats) {
                    stats.value = Object.assign(emptyStats(), data.stats);
                }
            } catch (e) { console.error(e); }
        };
        const refreshStats = async () => {
            statsLoading.value = true;
            try {
                await loadStats();
                statusText.value = '数据已刷新';
            } finally {
                statsLoading.value = false;
            }
        };

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
                        statusText.value = '登录成功！Cookie 已保存';
                        showToast('登录成功');
                        clearInterval(pollTimer);
                        pollTimer = null;
                        await loadStats();
                    }
                } catch (e) { console.error(e); }
            }, 3000);
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
                    await loadStats();
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
                await loadStats();
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
                await loadStats();
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
                await loadStats();
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
                configForm.quality = data.config.quality;
            }
        };
        const saveConfig = async () => {
            configLoading.value = true;
            try {
                const data = await apiPost('api/config/update', {
                    download_dir: configForm.download_dir,
                    max_size_mb: configForm.max_size_mb,
                    quality: configForm.quality,
                });
                if (data.success) {
                    statusText.value = '配置已保存';
                    showToast('配置已保存');
                    await loadStats();
                } else {
                    statusText.value = '保存失败：' + (data.error || '未知错误');
                }
            } catch (e) {
                statusText.value = '请求异常：' + e.message;
            } finally {
                configLoading.value = false;
            }
        };

        // ---------- FFmpeg ----------
        const loadFFmpegStatus = async () => {
            try {
                const data = await apiGet('api/ffmpeg/status');
                if (data.success) {
                    ffmpegStatus.value = data.status;
                }
            } catch (e) { console.error(e); }
        };

        const downloadFFmpeg = async () => {
            ffmpegStatus.value = { status: 'downloading', message: '正在下载中...' };
            try {
                const data = await apiPost('api/ffmpeg/download', {});
                if (data.success) {
                    showToast('下载任务已启动，请耐心等待');
                    const timer = setInterval(async () => {
                        await loadFFmpegStatus();
                        if (ffmpegStatus.value.status !== 'downloading') {
                            clearInterval(timer);
                            if (ffmpegStatus.value.status === 'ready') showToast('FFmpeg 配置成功');
                            else showToast('下载失败，请查看日志');
                            await loadStats();
                        }
                    }, 3000);
                } else {
                    showToast(data.error || '启动下载失败');
                    await loadFFmpegStatus();
                }
            } catch (e) {
                showToast('请求异常：' + e.message);
                await loadFFmpegStatus();
            }
        };

        onMounted(async () => {
            await Promise.all([loadStatus(), loadConfig(), loadFFmpegStatus(), loadStats()]);
            statsTimer = setInterval(loadStats, 30000);
            if (cookies.value.length === 0) {
                activeSection.value = 'qrcode';
                await generateQR();
            }
        });
        onUnmounted(() => {
            if (pollTimer) clearInterval(pollTimer);
            if (toastTimer) clearTimeout(toastTimer);
            if (statsTimer) clearInterval(statsTimer);
        });
        return {
            activeSection,
            statusText,
            qrUrl,
            isLoading,
            manualCookie,
            cookies,
            configForm,
            configLoading,
            showClearConfirm,
            showClearAllConfirm,
            toastMessage,
            toastVisible,
            ffmpegStatus,
            stats,
            statsLoading,
            typeRows,
            barHeight,
            formatBytes,
            formatTime,
            typeLabel,
            generateQR,
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
            downloadFFmpeg,
            refreshStats,
        };
    },
    template: TEMPLATE,
}).mount('#app');
