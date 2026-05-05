-- IntelliJ Classic / Zed-style keymap for users who don't want to learn vim motions.
-- Pair with ~/.config/ghostty/config so Cmd+<key> arrives as Esc+<key> (Alt-<key>).

local map = vim.keymap.set
local opts = { silent = true }

-- ── File / app ──────────────────────────────────────────────────────
map({ "i", "n", "v" }, "<C-s>", "<Esc><cmd>w<CR>", opts)
map({ "i", "n", "v" }, "<A-s>", "<Esc><cmd>w<CR>", opts) -- Cmd+S via ghostty
map({ "i", "n", "v" }, "<A-q>", "<Esc><cmd>q<CR>", opts) -- Cmd+Q
map({ "i", "n", "v" }, "<C-a>", "<Esc>ggVG", opts)
map({ "i", "n", "v" }, "<A-a>", "<Esc>ggVG", opts)       -- Cmd+A

-- ── Edit ────────────────────────────────────────────────────────────
map({ "i" }, "<C-z>", "<Esc>ua", opts)
map({ "n", "v" }, "<C-z>", "u", opts)
map({ "i" }, "<C-S-z>", "<Esc><C-r>a", opts)
map({ "n", "v" }, "<C-S-z>", "<C-r>", opts)

map({ "i", "n", "v" }, "<C-d>", "<Esc>yypi", opts)         -- Duplicate line
map({ "i", "n" }, "<C-y>", "<Esc>ddi", opts)               -- Delete line
map({ "i", "n", "v" }, "<C-/>", "<Esc>gcc", { remap = true, silent = true })
map("v", "<C-/>", "gc", { remap = true, silent = true })

-- Move line up/down (IntelliJ + Zed both use these)
map("n", "<C-S-Up>", "<cmd>m .-2<CR>==", opts)
map("n", "<C-S-Down>", "<cmd>m .+1<CR>==", opts)
map("i", "<C-S-Up>", "<Esc><cmd>m .-2<CR>==gi", opts)
map("i", "<C-S-Down>", "<Esc><cmd>m .+1<CR>==gi", opts)
map("v", "<C-S-Up>", ":m '<-2<CR>gv=gv", opts)
map("v", "<C-S-Down>", ":m '>+1<CR>gv=gv", opts)

-- ── Search ──────────────────────────────────────────────────────────
map({ "i", "n", "v" }, "<C-f>", "<Esc>/", { silent = false })
map({ "i", "n", "v" }, "<A-f>", "<Esc>/", { silent = false }) -- Cmd+F
map({ "i", "n", "v" }, "<C-S-f>", "<Esc><cmd>Telescope live_grep<CR>", opts)
map({ "i", "n", "v" }, "<C-r>", "<Esc>:%s/", { silent = false })

-- ── Navigation (IntelliJ Classic) ───────────────────────────────────
map({ "i", "n", "v" }, "<C-S-n>", "<Esc><cmd>Telescope find_files<CR>", opts)
map({ "i", "n", "v" }, "<C-n>", "<Esc><cmd>Telescope lsp_document_symbols<CR>", opts)
map({ "i", "n", "v" }, "<C-S-A-n>", "<Esc><cmd>Telescope lsp_workspace_symbols<CR>", opts)
map({ "i", "n", "v" }, "<C-S-a>", "<Esc><cmd>Telescope commands<CR>", opts)
map({ "i", "n", "v" }, "<C-g>", "<Esc>:", { silent = false }) -- IntelliJ Go-to-Line: type :42
map({ "i", "n", "v" }, "<C-e>", "<Esc><cmd>Telescope buffers<CR>", opts)

-- Tabs/buffers like IntelliJ Classic Ctrl-Right/Left
map({ "n", "v" }, "<C-Right>", "<cmd>bnext<CR>", opts)
map({ "n", "v" }, "<C-Left>", "<cmd>bprevious<CR>", opts)
map({ "i" }, "<C-Right>", "<Esc><cmd>bnext<CR>", opts)
map({ "i" }, "<C-Left>", "<Esc><cmd>bprevious<CR>", opts)

-- LSP go-to (IntelliJ uses Cmd+B; we mirror to Ctrl+B too)
map({ "n" }, "<C-b>", vim.lsp.buf.definition, opts)
map({ "n" }, "<C-S-b>", vim.lsp.buf.type_definition, opts)
map({ "n" }, "<C-A-b>", vim.lsp.buf.implementation, opts)
map({ "i", "n" }, "<F2>", vim.diagnostic.goto_next, opts)
map({ "i", "n" }, "<S-F2>", vim.diagnostic.goto_prev, opts)
map({ "n" }, "<S-F6>", vim.lsp.buf.rename, opts)
map({ "i", "n" }, "<A-CR>", vim.lsp.buf.code_action, opts)
map({ "i", "n" }, "<C-Space>", "<C-x><C-o>", opts)

-- ── Selection (Mac-style line / word ends) ──────────────────────────
-- Cmd+Left/Right are already remapped in ghostty to ^A / ^E (line start/end).
-- Cmd+Shift+Left/Right map to Shift+Home/End in ghostty — handled natively.
-- Ctrl+W = expand selection (IntelliJ classic) — see treesitter plugin override below.

-- ── Folding (IntelliJ Ctrl +/-) ─────────────────────────────────────
map({ "n", "v" }, "<C-=>", "zo", opts)
map({ "n", "v" }, "<C-->", "zc", opts)
map({ "n", "v" }, "<C-S-=>", "zR", opts)
map({ "n", "v" }, "<C-S-->", "zM", opts)
