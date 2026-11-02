"""Tk desktop GUI; blocking network and FFmpeg jobs run outside the GUI thread."""
from __future__ import annotations
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from .models import Collection, Cancelled, BiliAlbumError
from .resolver import Resolver
from .selection import select_episodes
from .quality import QUALITY_CHOICES, available_heights
from .downloader import DownloadManager, probe_episode
from .network import gui_error_message, safe_url


class BiliAlbumApp(tk.Tk):
    def __init__(self, *, direct=False):
        super().__init__()
        self.title('BiliAlbum · Bilibili 视频保存到苹果相册')
        self.geometry('1000x770')
        self.minsize(770, 610)
        self.collection: Collection | None = None
        self.worker: threading.Thread | None = None
        self._job_active = False
        self.manager: DownloadManager | None = None
        self.events: queue.Queue[tuple] = queue.Queue()
        self.url = tk.StringVar()
        self.mode = tk.StringVar(value='single')
        self.custom = tk.StringVar(value='1,3,5-8')
        self.quality = tk.StringVar(value=QUALITY_CHOICES[0][0])
        self.fallback = tk.BooleanVar(value=True)
        self.direct = tk.BooleanVar(value=direct)
        self.output = tk.StringVar(value=str(Path.home() / 'Downloads' / 'Bilibili'))
        self.summary = tk.StringVar(value='粘贴视频网址并点击「解析网址」')
        self.quality_hint = tk.StringVar(value='画质需在解析后检测。整套合集会逐集检查。')
        self.progress = tk.DoubleVar(value=0)
        self.state = tk.StringVar(value='就绪')
        self._make_gui()
        self.after(130, self._poll)

    def _make_gui(self):
        style = ttk.Style(self)
        if 'clam' in style.theme_names() and sys.platform != 'darwin':
            style.theme_use('clam')
        style.configure('Title.TLabel', font=('Arial', 17, 'bold'))
        style.configure('Sub.TLabel', foreground='#596473')
        style.configure('Action.TButton', padding=(12, 8))
        root = ttk.Frame(self, padding=18)
        root.pack(fill='both', expand=True)
        ttk.Label(root, text='BiliAlbum  ·  Bilibili 下载管理器', style='Title.TLabel').pack(anchor='w')
        ttk.Label(root, text='免费可访问画质 · 精确选集 · H.264 / AAC / MP4 · Apple 相册兼容', style='Sub.TLabel').pack(anchor='w', pady=(4, 14))
        input_bar = ttk.Frame(root)
        input_bar.pack(fill='x', pady=(0, 9))
        self.url_entry = ttk.Entry(input_bar, textvariable=self.url)
        self.url_entry.pack(side='left', fill='x', expand=True)
        self.url_entry.bind('<Return>', lambda e: self.resolve())
        ttk.Button(input_bar, text='粘贴', command=self.paste).pack(side='left', padx=(8, 4))
        self.parse_button = ttk.Button(input_bar, text='解析网址', command=self.resolve, style='Action.TButton')
        self.parse_button.pack(side='left')
        ttk.Label(root, textvariable=self.summary, wraplength=870).pack(anchor='w', pady=(3, 10))

        area = ttk.LabelFrame(root, text='视频 / 合集分集（显示顺序就是下载编号）', padding=(10, 8))
        area.pack(fill='both', expand=True)
        self.table = ttk.Treeview(area, columns=('n', 'title', 'date', 'bvid'), show='headings', selectmode='browse', height=9)
        self.table.heading('n', text='集数')
        self.table.heading('title', text='标题')
        self.table.heading('date', text='发布时间')
        self.table.heading('bvid', text='BV / P')
        self.table.column('n', width=64, anchor='center', stretch=False)
        self.table.column('title', width=490, stretch=True)
        self.table.column('date', width=112, anchor='center', stretch=False)
        self.table.column('bvid', width=165, stretch=False)
        scroll = ttk.Scrollbar(area, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')

        settings = ttk.Frame(root)
        settings.pack(fill='x', pady=(10, 4))
        modes = ttk.LabelFrame(settings, text='下载范围', padding=10)
        modes.pack(side='left', fill='both', expand=True, padx=(0, 8))
        ttk.Radiobutton(modes, text='仅下载选中的单集', variable=self.mode, value='single').pack(anchor='w')
        self.all_radio = ttk.Radiobutton(modes, text='下载整个合集', variable=self.mode, value='all')
        self.all_radio.pack(anchor='w')
        self.custom_radio = ttk.Radiobutton(modes, text='自定义编号：', variable=self.mode, value='custom')
        self.custom_radio.pack(anchor='w')
        ttk.Entry(modes, textvariable=self.custom, width=30).pack(anchor='w', fill='x', pady=3)
        ttk.Label(modes, text='例如 1,3,5-8；会自动排序、去重', style='Sub.TLabel').pack(anchor='w')

        right = ttk.LabelFrame(settings, text='画质与兼容性', padding=10)
        right.pack(side='left', fill='both', expand=True)
        self.quality_menu = ttk.Combobox(right, textvariable=self.quality, state='readonly',
                                         values=[label for label, _ in QUALITY_CHOICES], width=30)
        self.quality_menu.pack(fill='x', pady=(0, 7))
        ttk.Checkbutton(right, variable=self.fallback, text='画质不可用时允许降级').pack(anchor='w')
        ttk.Checkbutton(right, variable=self.direct, text='仅本程序直连（忽略系统/环境代理）').pack(anchor='w')
        ttk.Label(right, textvariable=self.quality_hint, wraplength=340, style='Sub.TLabel').pack(anchor='w', pady=(6, 3))
        ttk.Label(right, text='输出：MP4 · H.264 · AAC · yuv420p', style='Sub.TLabel').pack(anchor='w')

        folder = ttk.Frame(root)
        folder.pack(fill='x', pady=(9, 7))
        ttk.Label(folder, text='保存位置：').pack(side='left')
        ttk.Entry(folder, textvariable=self.output).pack(side='left', fill='x', expand=True, padx=(4, 6))
        ttk.Button(folder, text='浏览…', command=self.browse).pack(side='left')
        bar = ttk.Frame(root)
        bar.pack(fill='x', pady=(4, 6))
        self.start_button = ttk.Button(bar, text='开始下载', command=self.start, style='Action.TButton')
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(bar, text='取消当前任务', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=7)
        ttk.Button(bar, text='打开输出文件夹', command=self.open_folder).pack(side='left')
        ttk.Label(bar, textvariable=self.state).pack(side='right', padx=7)
        ttk.Progressbar(root, maximum=100, variable=self.progress).pack(fill='x', pady=(3, 7))
        logs = ttk.LabelFrame(root, text='运行日志', padding=6)
        logs.pack(fill='both', expand=False)
        self.log = tk.Text(logs, height=6, wrap='word', state='disabled', font=('Menlo', 10))
        self.log.pack(fill='both', expand=True)
        self._log('程序就绪。下载范围仅用于明确获得下载许可的视频。')

    def _log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', str(text) + '\n')
        self.log.see('end')
        if int(self.log.index('end-1c').split('.')[0]) > 450:
            self.log.delete('1.0', '100.0')
        self.log.configure(state='disabled')

    def paste(self):
        try:
            self.url.set(self.clipboard_get().strip())
        except tk.TclError:
            messagebox.showinfo('剪贴板', '剪贴板中没有可用文本。')

    def browse(self):
        chosen = filedialog.askdirectory(initialdir=str(Path.home()))
        if chosen:
            self.output.set(chosen)

    def open_folder(self):
        path = Path(self.output.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        import subprocess
        if sys.platform == 'darwin':
            subprocess.Popen(['open', str(path)])
        elif sys.platform == 'win32':
            import os
            os.startfile(str(path))
        else:
            subprocess.Popen(['xdg-open', str(path)])

    def _busy(self):
        return self._job_active or bool(self.worker and self.worker.is_alive())

    def resolve(self):
        if self._busy():
            return
        url = self.url.get().strip()
        if not url:
            messagebox.showwarning('缺少网址', '请先粘贴网址。')
            return
        self.collection = None
        self.summary.set('正在读取视频元数据…')
        self.quality_hint.set('尚未进入画质探测阶段。')
        self.table.delete(*self.table.get_children())
        self.state.set('正在解析…')
        self.progress.set(0)
        self.parse_button.configure(state='disabled')
        self.start_button.configure(state='disabled')
        self._log('解析：' + safe_url(url))
        direct = self.direct.get()
        self._log('网络：仅本程序直连' if direct else '网络：跟随系统/环境代理')
        self._job_active = True
        self.worker = threading.Thread(target=self._resolve_worker, args=(url, direct), daemon=True)
        self.worker.start()

    def _resolve_worker(self, url, direct=False):
        try:
            result = Resolver(direct=direct).resolve(url)
            self.events.put(('resolved', result))
            # Probe only selected episode, never assume every member has same quality.
            try:
                info = result.initial_info or probe_episode(result.episodes[result.selected_number - 1], direct=direct)
                self.events.put(('quality', available_heights(info)))
            except Exception as exc:
                self.events.put(('quality_error', gui_error_message(exc)))
        except Exception as exc:
            self.events.put(('resolve_error', gui_error_message(exc)))
        finally:
            self.events.put(('resolve_end',))

    def _apply_result(self, result: Collection):
        self.collection = result
        import datetime
        for ep in result.episodes:
            try:
                date = datetime.datetime.fromtimestamp(ep.published).strftime('%Y-%m-%d') if ep.published else '未知'
            except (ValueError, OverflowError, OSError):
                date = '未知'
            self.table.insert('', 'end', iid=str(ep.number), values=(ep.number, ep.title, date, f'{ep.bvid} / P{ep.page}'))
        self.table.selection_set(str(result.selected_number))
        self.table.see(str(result.selected_number))
        self.mode.set('single')
        self.all_radio.configure(state='normal' if result.is_collection else 'disabled')
        self.custom_radio.configure(state='normal' if result.is_collection else 'disabled')
        self.summary.set(f'{result.title} · {len(result.episodes)} 集 · {result.order_note}')
        self.state.set('解析完成')
        self._log(f'检测到 {result.kind} · {len(result.episodes)} 集；默认选中第 {result.selected_number} 集')
        self._log(result.order_note)
        self.start_button.configure(state='normal')

    def start(self):
        if self._busy():
            return
        if not self.collection:
            messagebox.showwarning('请解析网址', '请先点击「解析网址」。')
            return
        chosen = self.table.selection()
        index = int(chosen[0]) if chosen else self.collection.selected_number
        try:
            items = select_episodes(self.collection.episodes, self.mode.get(), index, self.custom.get())
        except BiliAlbumError as exc:
            messagebox.showerror('集数错误', str(exc))
            return
        if not items:
            return
        quality = dict(QUALITY_CHOICES).get(self.quality.get())
        if quality is None:
            messagebox.showerror('画质错误', '请选择一个有效画质。')
            return
        listing = '\n'.join(f'{e.number:03d}  {e.title}' for e in items[:12])
        if len(items) > 12:
            listing += f'\n……剩余 {len(items) - 12} 集'
        if not messagebox.askyesno('确认下载', f'共选择 {len(items)} 集：\n{listing}\n\n画质：{self.quality.get()}\n保存：{self.output.get()}\n\n仅适用于你有权下载的视频。是否开始？'):
            return
        self.manager = DownloadManager(Path(self.output.get()), lambda st, msg, p: self.events.put(('progress', st, msg, p)),
                                       direct=self.direct.get())
        self.start_button.configure(state='disabled')
        self.parse_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.progress.set(0)
        self._job_active = True
        self.worker = threading.Thread(target=self._download_worker,
                                       args=(self.collection, items, quality, self.fallback.get()), daemon=True)
        self.worker.start()

    def _download_worker(self, collection, items, quality, fallback):
        try:
            assert self.manager is not None
            result = self.manager.run(collection, items, quality, fallback)
            self.events.put(('done', result))
        except Cancelled as exc:
            self.events.put(('cancelled', str(exc)))
        except Exception as exc:
            self.events.put(('download_error', gui_error_message(exc, downloading=True)))
        finally:
            self.events.put(('download_end',))

    def stop(self):
        if self.manager:
            self.manager.cancel.set()
            self._log('正在请求取消。网络操作可能需要完成当前请求才能停止。')

    def _poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == 'resolved':
                    self._apply_result(event[1])
                elif kind == 'quality':
                    self.quality_hint.set('初始选中集检测到：' + (', '.join(f'{v}P' for v in event[1]) or '无') + '。下载时会逐集检测。')
                elif kind == 'quality_error':
                    self.quality_hint.set('画质预检失败；请先排查访问情况：' + event[1][:250])
                    self._log(self.quality_hint.get())
                elif kind == 'resolve_error':
                    self.collection = None
                    self.start_button.configure(state='disabled')
                    self.state.set('解析失败')
                    self.summary.set(event[1])
                    self._log('解析失败：' + event[1])
                    messagebox.showerror('解析失败', event[1])
                elif kind == 'resolve_end':
                    self._job_active = False
                    self.parse_button.configure(state='normal')
                elif kind == 'progress':
                    _, stage, msg, p = event
                    self.state.set(stage)
                    self.progress.set(p * 100)
                    if stage != '下载' and stage != '转换':
                        self._log(f'[{stage}] {msg}')
                elif kind == 'done':
                    successful, failed = event[1]
                    title = ('下载失败' if successful == 0 else '部分下载失败') if failed else '下载完成'
                    self.state.set(title)
                    self._log(f'任务结束：成功/已存在 {successful}，失败 {failed}')
                    popup = messagebox.showwarning if failed else messagebox.showinfo
                    popup(title, f'成功/已存在：{successful} 集\n失败：{failed} 集\n\n请查看输出目录与运行日志。')
                elif kind in ('cancelled', 'download_error'):
                    self.state.set('已取消' if kind == 'cancelled' else '下载失败')
                    self._log(event[1])
                    messagebox.showwarning('任务结束', event[1])
                elif kind == 'download_end':
                    self._job_active = False
                    self.start_button.configure(state='normal')
                    self.parse_button.configure(state='normal')
                    self.stop_button.configure(state='disabled')
        except queue.Empty:
            pass
        self.after(130, self._poll)


def start_gui(*, direct=False):
    BiliAlbumApp(direct=direct).mainloop()
