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
        <div class="sidebar-title">B站插件管理</div>
        <div class="nav-item" :class="{ active: activeSection === 'qrcode' }" @click="activeSection = 'qrcode'">扫码登录</div>
        <div class="nav-item" :class="{ active: activeSection === 'password' }" @click="activeSection = 'password'">账密登录</div>
        <div class="nav-item" :class="{ active: activeSection === 'cookie-input' }" @click="activeSection = 'cookie-input'">Cookie 输入</div>
        <div class="nav-item" :class="{ active: activeSection === 'cookie-manage' }" @click="activeSection = 'cookie-manage'">Cookie 管理</div>
        <div class="nav-item" :class="{ active: activeSection === 'config' }" @click="activeSection = 'config'">配置插件</div>
    </aside>

    <main class="content">
        <div class="status-bar">{{ statusText }}</div>

        <!-- 扫码登录 -->
        <section v-if="activeSection === 'qrcode'">
            <h2>扫码登录</h2>
            <div v-if="qrUrl" class="qr-box">
                <img :src="qrUrl" alt="二维码">
            </div>
            <button @click="generateQR" :disabled="isLoading">{{ isLoading ? '生成中...' : '重新生成二维码' }}</button>
        </section>

        <!-- 账密登录 -->
        <section v-if="activeSection === 'password'">
            <h2>账密登录</h2>
            <div class="form-group">
                <input v-model="passwordForm.username" type="text" placeholder="用户名/手机号/邮箱">
                <input v-model="passwordForm.password" type="password" placeholder="密码">
                <button @click="submitPasswordLogin" :disabled="passwordLoading">{{ passwordLoading ? '登录中...' : '登录' }}</button>
                <p class="hint">B站账密登录涉及复杂验证码，此功能暂不可用，请使用扫码登录。</p>
            </div>
        </section>

        <!-- Cookie 输入 -->
        <section v-if="activeSection === 'cookie-input'">
            <h2>手动添加 Cookie</h2>
            <div class="form-group">
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
        </section>

        <!-- 配置插件 -->
        <section v-if="activeSection === 'config'">
            <h2>配置插件</h2>
            <div class="form-group">
                <label>下载目录</label>
                <input v-model="configForm.download_dir" type="text">
                <label>最大文件大小 (MB)</label>
                <input v-model="configForm.max_size_mb" type="number" min="1">
                <label>机器人 QQ</label>
                <input v-model="configForm.bot_qq" type="text" placeholder="留空自动识别">
                <label>画质代码</label>
                <input v-model="configForm.quality" type="number" min="1">
                <button @click="saveConfig" :disabled="configLoading">{{ configLoading ? '保存中...' : '保存配置' }}</button>
            </div>
        </section>

        <!-- ASCII 装饰（位于右侧内容区底部） -->
        <pre>${ASCII_ART}</pre>
    </main>

    <!-- 删除确认弹窗 -->
    <div v-if="showClearConfirm" class="modal-overlay" @click.self="cancelDeleteCookie">
        <div class="modal-panel">
            <h3>⚠️ 警告</h3>
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
            <h3>⚠️ 严重警告</h3>
            <p>确定要清除所有 Cookie 吗？此操作不可撤销，所有账号登录状态将丢失。</p>
            <div class="modal-actions">
                <button @click="cancelClearAll">取消</button>
                <button class="danger" @click="confirmClearAll">确认全部清除</button>
            </div>
        </div>
    </div>
</div>
`;