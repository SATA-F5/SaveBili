const ASCII_ART = `
       ________ __    _____    __                  __   _        
      / ____/ //_/   / ___/   / /_   __  __   ____/ /  (_)  ____ 
     / / __/ ,<      \\__ \\   / __/  / / / /  / __  /  / /  / __ \\
    / /_/ / /| |    ___/ /  / /_   / /_/ /  / /_/ /  / /  / /_/ /
    \\____/_/ |_|   /____/   \\__/   \\__,_/   \\__,_/  /_/   \\____/ 
`;

export const TEMPLATE = `
<div class="manager-container">
    <aside class="sidebar">
        <div class="sidebar-title">SaveBili</div>
        <div class="nav-item" :class="{ active: activeSection === 'dashboard' }" @click="activeSection = 'dashboard'">数据概览</div>
        <div class="nav-item" :class="{ active: activeSection === 'qrcode' }" @click="activeSection = 'qrcode'">扫码登录</div>
        <div class="nav-item" :class="{ active: activeSection === 'cookie-input' }" @click="activeSection = 'cookie-input'">Cookie 输入</div>
        <div class="nav-item" :class="{ active: activeSection === 'cookie-manage' }" @click="activeSection = 'cookie-manage'">Cookie 管理</div>
        <div class="nav-item" :class="{ active: activeSection === 'config' }" @click="activeSection = 'config'">配置插件</div>
    </aside>
    <main class="content">
        <div class="status-bar">{{ statusText }}</div>

        <!-- 数据概览 -->
        <section v-if="activeSection === 'dashboard'" class="dashboard">
            <div class="section-head">
                <h2>数据概览</h2>
                <button class="ghost" @click="refreshStats" :disabled="statsLoading">{{ statsLoading ? '刷新中...' : '刷新数据' }}</button>
            </div>

            <div class="stat-grid">
                <div class="stat-card">
                    <div class="stat-label">今日下载</div>
                    <div class="stat-value">{{ stats.today.count }}</div>
                    <div class="stat-sub">成功 {{ stats.today.success }} · 失败 {{ stats.today.failed }}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">累计下载</div>
                    <div class="stat-value">{{ stats.totals.count }}</div>
                    <div class="stat-sub">累计流量 {{ formatBytes(stats.totals.bytes) }}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">成功率</div>
                    <div class="stat-value">{{ stats.success_rate }}<span class="stat-unit">%</span></div>
                    <div class="stat-sub">成功 {{ stats.totals.success }} · 失败 {{ stats.totals.failed }}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">下载目录</div>
                    <div class="stat-value">{{ stats.storage.files }}<span class="stat-unit">个文件</span></div>
                    <div class="stat-sub">占用 {{ formatBytes(stats.storage.bytes) }}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">可用 Cookie</div>
                    <div class="stat-value">{{ stats.cookies.enabled }}<span class="stat-unit">/ {{ stats.cookies.total }}</span></div>
                    <div class="stat-sub">{{ stats.cookies.enabled > 0 ? '高清画质已启用' : '未登录，仅能下载低清' }}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">FFmpeg</div>
                    <div class="stat-value stat-small" :class="ffmpegStatus.status === 'ready' ? 'ok' : 'warn'">
                        {{ ffmpegStatus.status === 'ready' ? '就绪' : (ffmpegStatus.status === 'downloading' ? '下载中' : '未就绪') }}
                    </div>
                    <div class="stat-sub">{{ ffmpegStatus.message }}</div>
                </div>
            </div>

            <div class="panel-grid">
                <div class="glass-card">
                    <h2>近 7 天下载趋势</h2>
                    <div class="chart">
                        <div v-for="d in stats.days" :key="d.date" class="chart-col">
                            <div class="chart-bar-wrap" :title="d.date + '：' + d.count + ' 次'">
                                <div class="chart-bar" :style="{ height: barHeight(d.count) }"></div>
                            </div>
                            <div class="chart-value">{{ d.count }}</div>
                            <div class="chart-label">{{ d.label }}</div>
                        </div>
                    </div>
                </div>

                <div class="glass-card">
                    <h2>类型分布</h2>
                    <div v-if="typeRows.length === 0" class="empty">暂无下载数据</div>
                    <div v-for="t in typeRows" :key="t.key" class="type-row">
                        <span class="type-name">{{ t.label }}</span>
                        <div class="type-track"><div class="type-fill" :style="{ width: t.pct + '%' }"></div></div>
                        <span class="type-count">{{ t.count }}</span>
                    </div>
                </div>
            </div>

            <div class="glass-card">
                <h2>最近下载</h2>
                <table class="recent-table" v-if="stats.recent.length > 0">
                    <thead>
                        <tr><th>时间</th><th>类型</th><th>标题</th><th>大小</th><th>状态</th></tr>
                    </thead>
                    <tbody>
                        <tr v-for="(r, i) in stats.recent" :key="i">
                            <td class="mono">{{ formatTime(r.ts) }}</td>
                            <td>{{ typeLabel(r.type) }}</td>
                            <td class="title-cell" :title="r.title">{{ r.title || '（无标题）' }}</td>
                            <td class="mono">{{ r.bytes ? formatBytes(r.bytes) : '-' }}</td>
                            <td><span class="pill" :class="r.ok ? 'ok' : 'bad'">{{ r.ok ? '成功' : '失败' }}</span></td>
                        </tr>
                    </tbody>
                </table>
                <div v-else class="empty">还没有下载记录，发个 B 站链接试试</div>
            </div>
        </section>

        <!-- 扫码登录 -->
        <section v-if="activeSection === 'qrcode'">
            <div class="glass-card">
                <h2>扫码登录</h2>
                <p class="hint">使用 B 站 App 扫码，登录后自动保存 Cookie，可下载更高画质。</p>
                <div v-if="qrUrl" class="qr-box">
                    <img :src="qrUrl" alt="二维码">
                </div>
                <button @click="generateQR" :disabled="isLoading">{{ isLoading ? '生成中...' : '重新生成二维码' }}</button>
            </div>
        </section>

        <!-- Cookie 输入 -->
        <section v-if="activeSection === 'cookie-input'">
            <div class="form-group">
                <h2>手动添加 Cookie</h2>
                <label>Cookie 字符串</label>
                <textarea v-model="manualCookie.cookie" rows="4" placeholder="粘贴完整 Cookie 字符串（如 SESSDATA=xxx; bili_jct=yyy），或直接输入 SESSDATA 值（不含 SESSDATA= 前缀）"></textarea>
                <label>备注</label>
                <input v-model="manualCookie.remark" type="text" placeholder="例如：大会员账号">
                <label>优先级（数字越小越优先）</label>
                <input v-model="manualCookie.priority" type="number" min="1" max="999">
                <label>
                    <input type="checkbox" v-model="manualCookie.enabled"> 启用
                </label>
                <button @click="addManualCookie">添加 Cookie</button>
            </div>
        </section>

        <!-- Cookie 管理 -->
        <section v-if="activeSection === 'cookie-manage'">
            <div class="glass-card">
                <h2>Cookie 管理</h2>
                <button class="danger" @click="requestClearAll" v-if="cookies.length > 0">清除全部 Cookie</button>
                <div v-if="cookies.length === 0" class="empty">暂无 Cookie</div>
                <div v-for="(c, index) in cookies" :key="index" class="cookie-item">
                    <div class="cookie-info">
                        <div class="cookie-remark">{{ c.remark || '未命名' }}</div>
                        <div class="cookie-preview">{{ c.cookie.substring(0, 50) }}...</div>
                        <div class="cookie-meta">
                            <label>优先级: <input type="number" :value="c.priority" @change="updatePriority(index, $event.target.value)"></label>
                            <label><input type="checkbox" :checked="c.enabled" @change="toggleCookie(index)"> 启用</label>
                        </div>
                    </div>
                    <button class="danger" @click="requestDeleteCookie(index)">删除</button>
                </div>
            </div>
        </section>

        <!-- 配置插件 -->
        <section v-if="activeSection === 'config'">
            <div class="form-group">
                <h2>配置插件</h2>
                <label>下载目录</label>
                <input v-model="configForm.download_dir" type="text">
                <label>最大文件大小 (MB)</label>
                <input v-model="configForm.max_size_mb" type="number" min="1">
                <label>画质代码</label>
                <input v-model="configForm.quality" type="number" min="1">
                <button @click="saveConfig" :disabled="configLoading">{{ configLoading ? '保存中...' : '保存配置' }}</button>
            </div>

            <div class="form-group" style="margin-top: 20px;">
                <h2>FFmpeg 环境配置</h2>
                <p class="hint">FFmpeg 用于合并 DASH 格式的分离音视频流，缺少它视频将无声。</p>
                <div style="margin: 10px 0; padding: 10px; background: rgba(0,0,0,0.2); border-radius: 8px;">
                    <span>当前状态：</span>
                    <span :style="{ color: ffmpegStatus.status === 'ready' ? '#00d2ff' : (ffmpegStatus.status === 'error' ? '#ff416c' : '#aaa') }">
                        {{ ffmpegStatus.message }}
                    </span>
                </div>
                <button @click="downloadFFmpeg" :disabled="ffmpegStatus.status === 'downloading'">
                    {{ ffmpegStatus.status === 'downloading' ? '正在下载中，请稍候...' : '一键下载 FFmpeg' }}
                </button>
            </div>
        </section>
    </main>
    <!-- ASCII 水印背景 -->
    <pre class="ascii-art">${ASCII_ART}</pre>
    <!-- Toast 通知 -->
    <div class="toast" :class="{ show: toastVisible }">{{ toastMessage }}</div>
    <!-- 删除确认弹窗 -->
    <div v-if="showClearConfirm" class="modal-overlay" @click.self="cancelDeleteCookie">
        <div class="modal-panel">
            <h3>警告</h3>
            <p>确定要删除这条 Cookie 吗？删除后可能影响高清视频下载。</p>
            <div class="modal-actions">
                <button @click="cancelDeleteCookie">取消</button>
                <button class="danger" @click="confirmDeleteCookie">确认删除</button>
            </div>
        </div>
    </div>
    <!-- 清除全部确认弹窗 -->
    <div v-if="showClearAllConfirm" class="modal-overlay" @click.self="cancelClearAll">
        <div class="modal-panel">
            <h3>严重警告</h3>
            <p>确定要清除所有 Cookie 吗？此操作不可撤销，所有账号登录状态将丢失。</p>
            <div class="modal-actions">
                <button @click="cancelClearAll">取消</button>
                <button class="danger" @click="confirmClearAll">确认全部清除</button>
            </div>
        </div>
    </div>
</div>
`;
