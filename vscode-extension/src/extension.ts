// SPDX-License-Identifier: GPL-3.0-or-later

import { execFile } from 'child_process';
import { promises as fs } from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as vscode from 'vscode';

const CHAT_SECTION = 'ai-features.chat';
const SCOPE_KEY = 'sessionStorageScope';
const SHOW_CHATS_COMMAND = 'ai-chat-ui.show-chats';
const RECENT_WRITE_MS = 10_000;
const SETTLE_MS = 500;
const MAX_ATTEMPTS = 3;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;
const LAST_BUNDLE_DIR_KEY = 'lastBundleDir';

interface Store {
    label: string;
    dir: string;
}

let output: vscode.OutputChannel;
let state: vscode.Memento;

export function activate(context: vscode.ExtensionContext): void {
    output = vscode.window.createOutputChannel('Theia Chat Transfer');
    state = context.globalState;
    context.subscriptions.push(
        output,
        vscode.commands.registerCommand('theiaChatTransfer.export', () => run(exportChats)),
        vscode.commands.registerCommand('theiaChatTransfer.import', () => run(importChats)),
        vscode.commands.registerCommand('theiaChatTransfer.reloadChatList', () => run(reloadChatList))
    );
}

export function deactivate(): void { }

async function run(action: () => Promise<void>): Promise<void> {
    output.show(true);
    try {
        await action();
    } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        output.appendLine(`ERROR: ${message}`);
        const choice = await vscode.window.showErrorMessage(`Theia Chat Transfer: ${message}`, 'Show Log');
        if (choice) {
            output.show();
        }
    }
}

function settings(): vscode.WorkspaceConfiguration {
    return vscode.workspace.getConfiguration('theiaChatTransfer');
}

function configDir(): string {
    const dir = settings().get<string>('configDir') || process.env.THEIA_CONFIG_DIR || path.join(os.homedir(), '.theia-ide');
    return dir.replace(/^~(?=$|\/)/, os.homedir());
}

function runCli(args: string[]): Promise<string> {
    const cli = settings().get<string>('cliPath') || 'theia-chat-transfer';
    const fullArgs = ['--config-dir', configDir(), ...args];
    output.appendLine(`$ ${cli} ${fullArgs.join(' ')}`);
    return new Promise((resolve, reject) => {
        // execFile does not use a shell, so file names cannot inject commands.
        execFile(cli, fullArgs, { timeout: 120_000 }, (error, stdout, stderr) => {
            output.append(stdout);
            output.append(stderr);
            if (error) {
                reject(new Error(`${cli} failed: ${stderr.trim() || error.message}`));
            } else {
                resolve(stdout);
            }
        });
    });
}

function lastBundleDir(): string {
    return state.get<string>(LAST_BUNDLE_DIR_KEY) ?? os.homedir();
}

async function rememberBundleDir(bundle: vscode.Uri): Promise<void> {
    await state.update(LAST_BUNDLE_DIR_KEY, path.dirname(bundle.fsPath));
}

async function exportChats(): Promise<void> {
    const now = new Date();
    const stamp = [now.getFullYear(), now.getMonth() + 1, now.getDate()].map(n => String(n).padStart(2, '0')).join('-');
    const target = await vscode.window.showSaveDialog({
        defaultUri: vscode.Uri.file(path.join(lastBundleDir(), `theia-chats-${stamp}.tar.gz`)),
        filters: { 'Chat bundles': ['gz'] },
        saveLabel: 'Export'
    });
    if (!target) {
        return;
    }
    await runCli(['export', target.fsPath]);
    await rememberBundleDir(target);
    vscode.window.showInformationMessage(`Exported chats to ${target.fsPath}`);
}

async function importChats(): Promise<void> {
    const picked = await vscode.window.showOpenDialog({
        defaultUri: vscode.Uri.file(lastBundleDir()),
        canSelectMany: false,
        filters: { 'Chat bundles': ['gz'] },
        openLabel: 'Import'
    });
    if (!picked?.length) {
        return;
    }
    await rememberBundleDir(picked[0]);
    const args = ['import', picked[0].fsPath];
    for (const mapping of settings().get<string[]>('pathMaps') ?? []) {
        args.push('--map', mapping);
    }

    await runCli([...args, '--dry-run']);
    const confirmed = await vscode.window.showWarningMessage(
        'Import these chats? The output panel shows what will change. Existing chats are backed up first.',
        { modal: true }, 'Import');
    if (confirmed !== 'Import') {
        return;
    }

    // An autosave writes Theia's cached index.json over ours, so avoid importing while a reply streams.
    if (Date.now() - await newestSessionWrite() < RECENT_WRITE_MS) {
        const choice = await vscode.window.showWarningMessage(
            'A chat was saved in the last few seconds, so a reply may still be running. Wait for it to finish, or import anyway.',
            { modal: true }, 'Import Anyway');
        if (choice !== 'Import Anyway') {
            return;
        }
    }

    const alreadyMissing = new Set(await sessionsMissingFromIndex());
    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
        // Re-running the import is safe: it keeps newer chats and rebuilds each index from the chat files.
        await runCli(args);
        await reloadChatList();
        const missing = (await sessionsMissingFromIndex()).filter(id => !alreadyMissing.has(id));
        if (missing.length === 0) {
            const choice = await vscode.window.showInformationMessage('Chats imported.', 'Show Chats');
            if (choice) {
                await vscode.commands.executeCommand(SHOW_CHATS_COMMAND);
            }
            return;
        }
        output.appendLine(`Attempt ${attempt}: ${missing.length} chat(s) missing from the chat list, probably overwritten by an autosave.`);
    }
    throw new Error('Theia kept overwriting the chat list. Close Theia and run the import from a terminal.');
}

/**
 * Theia caches each chat store's index.json in memory and only re-reads it when
 * `ai-features.chat.sessionStorageScope` changes. Flip the setting and restore it.
 */
async function reloadChatList(): Promise<void> {
    const config = vscode.workspace.getConfiguration(CHAT_SECTION);
    const info = config.inspect<string>(SCOPE_KEY);
    // Change the setting where its effective value comes from, or Theia sees no change.
    const target = info?.workspaceValue !== undefined ? vscode.ConfigurationTarget.Workspace : vscode.ConfigurationTarget.Global;
    const original = target === vscode.ConfigurationTarget.Workspace ? info?.workspaceValue : info?.globalValue;
    const other = config.get<string>(SCOPE_KEY, 'workspace') === 'global' ? 'workspace' : 'global';
    output.appendLine(`Reloading the chat list: ${CHAT_SECTION}.${SCOPE_KEY} -> ${other} and back`);
    try {
        await config.update(SCOPE_KEY, other, target);
        await delay(SETTLE_MS);
    } finally {
        // `undefined` removes the key again if it was not set before.
        await config.update(SCOPE_KEY, original, target);
        await delay(SETTLE_MS);
    }
}

async function findStores(): Promise<Store[]> {
    const dir = configDir();
    const stores: Store[] = [{ label: '(global)', dir: path.join(dir, 'chatSessions') }];
    const workspaces = await readJson<Record<string, string>>(path.join(dir, 'workspace-metadata', 'index.json')) ?? {};
    for (const [workspace, uuid] of Object.entries(workspaces)) {
        if (typeof uuid === 'string' && SAFE_ID.test(uuid)) {
            stores.push({ label: workspace, dir: path.join(dir, 'workspace-metadata', uuid, 'chatSessions') });
        }
    }
    return stores;
}

async function sessionFiles(store: Store): Promise<string[]> {
    try {
        const names = await fs.readdir(store.dir);
        return names.filter(name => name.endsWith('.json') && name !== 'index.json');
    } catch (error) {
        if (isNotFound(error)) {
            return [];
        }
        throw error;
    }
}

async function sessionsMissingFromIndex(): Promise<string[]> {
    const missing: string[] = [];
    for (const store of await findStores()) {
        const index = await readJson<Record<string, unknown>>(path.join(store.dir, 'index.json')) ?? {};
        for (const name of await sessionFiles(store)) {
            const id = name.slice(0, -'.json'.length);
            if (!(id in index)) {
                missing.push(`${store.label}: ${id}`);
            }
        }
    }
    return missing;
}

async function newestSessionWrite(): Promise<number> {
    let newest = 0;
    for (const store of await findStores()) {
        for (const name of await sessionFiles(store)) {
            const stat = await fs.stat(path.join(store.dir, name)).catch(() => undefined);
            newest = Math.max(newest, stat?.mtimeMs ?? 0);
        }
    }
    return newest;
}

async function readJson<T>(file: string): Promise<T | undefined> {
    try {
        return JSON.parse(await fs.readFile(file, 'utf8')) as T;
    } catch (error) {
        if (isNotFound(error)) {
            return undefined;
        }
        throw error;
    }
}

function isNotFound(error: unknown): boolean {
    return (error as NodeJS.ErrnoException)?.code === 'ENOENT';
}

function delay(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
}
