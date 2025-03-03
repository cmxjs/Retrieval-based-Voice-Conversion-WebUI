import os
import sys
from dotenv import load_dotenv

load_dotenv()

os.environ["OMP_NUM_THREADS"] = "4"
if sys.platform == "darwin":
    os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

now_dir = os.getcwd()
sys.path.append(now_dir)
import multiprocessing

flag_vc = False


def printt(strr, *args):
    if len(args) == 0:
        print(strr)
    else:
        print(strr % args)


def phase_vocoder(a, b, fade_out, fade_in):
    window = torch.sqrt(fade_out * fade_in)
    fa = torch.fft.rfft(a * window)
    fb = torch.fft.rfft(b * window)
    absab = torch.abs(fa) + torch.abs(fb)
    n = a.shape[0]
    if n % 2 == 0:
        absab[1:-1] *= 2
    else:
        absab[1:] *= 2
    phia = torch.angle(fa)
    phib = torch.angle(fb)
    deltaphase = phib - phia
    deltaphase = deltaphase - 2 * np.pi * torch.floor(deltaphase / 2 / np.pi + 0.5)
    w = 2 * np.pi * torch.arange(n // 2 + 1).to(a) + deltaphase
    t = torch.arange(n).unsqueeze(-1).to(a) / n
    result = (
        a * (fade_out**2)
        + b * (fade_in**2)
        + torch.sum(absab * torch.cos(w * t + phia), -1) * window / n
    )
    return result


class Harvest(multiprocessing.Process):
    def __init__(self, inp_q, opt_q):
        multiprocessing.Process.__init__(self)
        self.inp_q = inp_q
        self.opt_q = opt_q

    def run(self):
        import numpy as np
        import pyworld

        while 1:
            idx, x, res_f0, n_cpu, ts = self.inp_q.get()
            f0, t = pyworld.harvest(
                x.astype(np.double),
                fs=16000,
                f0_ceil=1100,
                f0_floor=50,
                frame_period=10,
            )
            res_f0[idx] = f0
            if len(res_f0.keys()) >= n_cpu:
                self.opt_q.put(ts)


if __name__ == "__main__":
    import json
    import multiprocessing
    import re
    import threading
    import time
    import traceback
    from multiprocessing import Queue, cpu_count
    from queue import Empty

    import librosa
    from tools.torchgate import TorchGate
    import numpy as np
    import PySimpleGUI as sg
    import sounddevice as sd
    import torch
    import torch.nn.functional as F
    import torchaudio.transforms as tat

    import tools.rvc_for_realtime as rvc_for_realtime
    from i18n.i18n import I18nAuto
    from configs.config import Config

    i18n = I18nAuto()

    # device = rvc_for_realtime.config.device
    # device = torch.device(
    #     "cuda"
    #     if torch.cuda.is_available()
    #     else ("mps" if torch.backends.mps.is_available() else "cpu")
    # )
    current_dir = os.getcwd()
    inp_q = Queue()
    opt_q = Queue()
    n_cpu = min(cpu_count(), 8)
    for _ in range(n_cpu):
        Harvest(inp_q, opt_q).start()

    class GUIConfig:
        def __init__(self) -> None:
            self.pth_path: str = ""
            self.index_path: str = ""
            self.pitch: int = 0
            self.sr_type: str = "sr_model"
            self.block_time: float = 0.25  # s
            self.threhold: int = -60
            self.crossfade_time: float = 0.05
            self.extra_time: float = 2.5
            self.I_noise_reduce: bool = False
            self.O_noise_reduce: bool = False
            self.use_pv: bool = False
            self.rms_mix_rate: float = 0.0
            self.index_rate: float = 0.0
            self.n_cpu: int = min(n_cpu, 4)
            self.f0method: str = "fcpe"
            self.sg_input_device: str = ""
            self.sg_output_device: str = ""

    class GUI:
        def __init__(self) -> None:
            self.gui_config = GUIConfig()
            self.config = Config()
            self.function = "vc"
            self.delay_time = 0
            self.input_devices = None
            self.output_devices = None
            self.input_devices_indices = None
            self.output_devices_indices = None
            self.stream = None
            self.update_devices()
            self.launcher()

        def load(self):
            try:
                with open("configs/config.json", "r") as j:
                    data = json.load(j)
                    data["sr_model"] = data["sr_type"] == "sr_model"
                    data["sr_device"] = data["sr_type"] == "sr_device"
                    data["pm"] = data["f0method"] == "pm"
                    data["harvest"] = data["f0method"] == "harvest"
                    data["crepe"] = data["f0method"] == "crepe"
                    data["rmvpe"] = data["f0method"] == "rmvpe"
                    data["fcpe"] = data["f0method"] == "fcpe"
                    if data["sg_input_device"] not in self.input_devices:
                        data["sg_input_device"] = self.input_devices[
                            self.input_devices_indices.index(sd.default.device[0])
                        ]
                    if data["sg_output_device"] not in self.output_devices:
                        data["sg_output_device"] = self.output_devices[
                            self.output_devices_indices.index(sd.default.device[1])
                        ]
            except:
                with open("configs/config.json", "w") as j:
                    data = {
                        "pth_path": "",
                        "index_path": "",
                        "sg_input_device": self.input_devices[
                            self.input_devices_indices.index(sd.default.device[0])
                        ],
                        "sg_output_device": self.output_devices[
                            self.output_devices_indices.index(sd.default.device[1])
                        ],
                        "sr_type": "sr_model",
                        "threhold": -60,
                        "pitch": 0,
                        "index_rate": 0,
                        "rms_mix_rate": 0,
                        "block_time": 0.25,
                        "crossfade_length": 0.05,
                        "extra_time": 2.5,
                        "n_cpu": 4,
                        "f0method": "rmvpe",
                        "use_jit": False,
                        "use_pv": False,
                    }
                    data["sr_model"] = data["sr_type"] == "sr_model"
                    data["sr_device"] = data["sr_type"] == "sr_device"
                    data["pm"] = data["f0method"] == "pm"
                    data["harvest"] = data["f0method"] == "harvest"
                    data["crepe"] = data["f0method"] == "crepe"
                    data["rmvpe"] = data["f0method"] == "rmvpe"
                    data["fcpe"] = data["f0method"] == "fcpe"
                    json.dump(data, j)
            return data

        def launcher(self):
            data = self.load()
            self.config.use_jit = False  # data.get("use_jit", self.config.use_jit)
            sg.theme("LightBlue3")
            layout = [
                [
                    sg.Frame(
                        title=i18n("加载模型"),
                        layout=[
                            [
                                sg.Input(
                                    default_text=data.get("pth_path", ""),
                                    key="pth_path",
                                ),
                                sg.FileBrowse(
                                    i18n("选择.pth文件"),
                                    initial_folder=os.path.join(
                                        os.getcwd(), "assets/weights"
                                    ),
                                    file_types=((". pth"),),
                                ),
                            ],
                            [
                                sg.Input(
                                    default_text=data.get("index_path", ""),
                                    key="index_path",
                                ),
                                sg.FileBrowse(
                                    i18n("选择.index文件"),
                                    initial_folder=os.path.join(os.getcwd(), "logs"),
                                    file_types=((". index"),),
                                ),
                            ],
                        ],
                    )
                ],
                [
                    sg.Frame(
                        layout=[
                            [
                                sg.Text(i18n("输入设备")),
                                sg.Combo(
                                    self.input_devices,
                                    key="sg_input_device",
                                    default_value=data.get("sg_input_device", ""),
                                ),
                            ],
                            [
                                sg.Text(i18n("输出设备")),
                                sg.Combo(
                                    self.output_devices,
                                    key="sg_output_device",
                                    default_value=data.get("sg_output_device", ""),
                                ),
                            ],
                            [
                                sg.Button(i18n("重载设备列表"), key="reload_devices"),
                                sg.Radio(
                                    i18n("使用模型采样率"),
                                    "sr_type",
                                    key="sr_model",
                                    default=data.get("sr_model", True),
                                    enable_events=True,
                                ),
                                sg.Radio(
                                    i18n("使用设备采样率"),
                                    "sr_type",
                                    key="sr_device",
                                    default=data.get("sr_device", False),
                                    enable_events=True,
                                ),
                                sg.Text(i18n("采样率:")),
                                sg.Text("", key="sr_stream"),
                            ],
                        ],
                        title=i18n("音频设备(请使用同种类驱动)"),
                    )
                ],
                [
                    sg.Frame(
                        layout=[
                            [
                                sg.Text(i18n("响应阈值")),
                                sg.Slider(
                                    range=(-60, 0),
                                    key="threhold",
                                    resolution=1,
                                    orientation="h",
                                    default_value=data.get("threhold", -60),
                                    enable_events=True,
                                ),
                            ],
                            [
                                sg.Text(i18n("音调设置")),
                                sg.Slider(
                                    range=(-24, 24),
                                    key="pitch",
                                    resolution=1,
                                    orientation="h",
                                    default_value=data.get("pitch", 0),
                                    enable_events=True,
                                ),
                            ],
                            [
                                sg.Text(i18n("Index Rate")),
                                sg.Slider(
                                    range=(0.0, 1.0),
                                    key="index_rate",
                                    resolution=0.01,
                                    orientation="h",
                                    default_value=data.get("index_rate", 0),
                                    enable_events=True,
                                ),
                            ],
                            [
                                sg.Text(i18n("响度因子")),
                                sg.Slider(
                                    range=(0.0, 1.0),
                                    key="rms_mix_rate",
                                    resolution=0.01,
                                    orientation="h",
                                    default_value=data.get("rms_mix_rate", 0),
                                    enable_events=True,
                                ),
                            ],
                            [
                                sg.Text(i18n("音高算法")),
                                sg.Radio(
                                    "pm",
                                    "f0method",
                                    key="pm",
                                    default=data.get("pm", False),
                                    enable_events=True,
                                ),
                                sg.Radio(
                                    "harvest",
                                    "f0method",
                                    key="harvest",
                                    default=data.get("harvest", False),
                                    enable_events=True,
                                ),
                                sg.Radio(
                                    "crepe",
                                    "f0method",
                                    key="crepe",
                                    default=data.get("crepe", False),
                                    enable_events=True,
                                ),
                                sg.Radio(
                                    "rmvpe",
                                    "f0method",
                                    key="rmvpe",
                                    default=data.get("rmvpe", False),
                                    enable_events=True,
                                ),
                                sg.Radio(
                                    "fcpe",
                                    "f0method",
                                    key="fcpe",
                                    default=data.get("fcpe", True),
                                    enable_events=True,
                                ),
                            ],
                        ],
                        title=i18n("常规设置"),
                    ),
                    sg.Frame(
                        layout=[
                            [
                                sg.Text(i18n("采样长度")),
                                sg.Slider(
                                    range=(0.02, 2.4),
                                    key="block_time",
                                    resolution=0.01,
                                    orientation="h",
                                    default_value=data.get("block_time", 0.25),
                                    enable_events=True,
                                ),
                            ],
                            # [
                            #     sg.Text("设备延迟"),
                            #     sg.Slider(
                            #         range=(0, 1),
                            #         key="device_latency",
                            #         resolution=0.001,
                            #         orientation="h",
                            #         default_value=data.get("device_latency", 0.1),
                            #         enable_events=True,
                            #     ),
                            # ],
                            [
                                sg.Text(i18n("harvest进程数")),
                                sg.Slider(
                                    range=(1, n_cpu),
                                    key="n_cpu",
                                    resolution=1,
                                    orientation="h",
                                    default_value=data.get(
                                        "n_cpu", min(self.gui_config.n_cpu, n_cpu)
                                    ),
                                    enable_events=True,
                                ),
                            ],
                            [
                                sg.Text(i18n("淡入淡出长度")),
                                sg.Slider(
                                    range=(0.01, 0.15),
                                    key="crossfade_length",
                                    resolution=0.01,
                                    orientation="h",
                                    default_value=data.get("crossfade_length", 0.05),
                                    enable_events=True,
                                ),
                            ],
                            [
                                sg.Text(i18n("额外推理时长")),
                                sg.Slider(
                                    range=(0.05, 5.00),
                                    key="extra_time",
                                    resolution=0.01,
                                    orientation="h",
                                    default_value=data.get("extra_time", 2.5),
                                    enable_events=True,
                                ),
                            ],
                            [
                                sg.Checkbox(
                                    i18n("输入降噪"),
                                    key="I_noise_reduce",
                                    enable_events=True,
                                ),
                                sg.Checkbox(
                                    i18n("输出降噪"),
                                    key="O_noise_reduce",
                                    enable_events=True,
                                ),
                                sg.Checkbox(
                                    i18n("启用相位声码器"),
                                    key="use_pv",
                                    default=data.get("use_pv", False),
                                    enable_events=True,
                                ),
                                # sg.Checkbox(
                                #     "JIT加速",
                                #     default=self.config.use_jit,
                                #     key="use_jit",
                                #     enable_events=False,
                                # ),
                            ],
                            # [sg.Text("注：首次使用JIT加速时，会出现卡顿，\n      并伴随一些噪音，但这是正常现象！")],
                        ],
                        title=i18n("性能设置"),
                    ),
                ],
                [
                    sg.Button(i18n("开始音频转换"), key="start_vc"),
                    sg.Button(i18n("停止音频转换"), key="stop_vc"),
                    sg.Radio(
                        i18n("输入监听"),
                        "function",
                        key="im",
                        default=False,
                        enable_events=True,
                    ),
                    sg.Radio(
                        i18n("输出变声"),
                        "function",
                        key="vc",
                        default=True,
                        enable_events=True,
                    ),
                    sg.Text(i18n("算法延迟(ms):")),
                    sg.Text("0", key="delay_time"),
                    sg.Text(i18n("推理时间(ms):")),
                    sg.Text("0", key="infer_time"),
                ],
            ]
            self.window = sg.Window("RVC - GUI", layout=layout, finalize=True)
            self.event_handler()

        def event_handler(self):
            global flag_vc
            while True:
                event, values = self.window.read()
                if event == sg.WINDOW_CLOSED:
                    self.stop_stream()
                    exit()
                if event == "reload_devices":
                    self.update_devices()
                    if self.gui_config.sg_input_device not in self.input_devices:
                        self.gui_config.sg_input_device = self.input_devices[0]
                    self.window["sg_input_device"].Update(values=self.input_devices)
                    self.window["sg_input_device"].Update(
                        value=self.gui_config.sg_input_device
                    )
                    if self.gui_config.sg_output_device not in self.output_devices:
                        self.gui_config.sg_output_device = self.output_devices[0]
                    self.window["sg_output_device"].Update(values=self.output_devices)
                    self.window["sg_output_device"].Update(
                        value=self.gui_config.sg_output_device
                    )
                if event == "start_vc" and not flag_vc:
                    if self.set_values(values) == True:
                        printt("cuda_is_available: %s", torch.cuda.is_available())
                        self.start_vc()
                        settings = {
                            "pth_path": values["pth_path"],
                            "index_path": values["index_path"],
                            "sg_input_device": values["sg_input_device"],
                            "sg_output_device": values["sg_output_device"],
                            "sr_type": ["sr_model", "sr_device"][
                                [
                                    values["sr_model"],
                                    values["sr_device"],
                                ].index(True)
                            ],
                            "threhold": values["threhold"],
                            "pitch": values["pitch"],
                            "rms_mix_rate": values["rms_mix_rate"],
                            "index_rate": values["index_rate"],
                            # "device_latency": values["device_latency"],
                            "block_time": values["block_time"],
                            "crossfade_length": values["crossfade_length"],
                            "extra_time": values["extra_time"],
                            "n_cpu": values["n_cpu"],
                            # "use_jit": values["use_jit"],
                            "use_jit": False,
                            "use_pv": values["use_pv"],
                            "f0method": ["pm", "harvest", "crepe", "rmvpe", "fcpe"][
                                [
                                    values["pm"],
                                    values["harvest"],
                                    values["crepe"],
                                    values["rmvpe"],
                                    values["fcpe"],
                                ].index(True)
                            ],
                        }
                        with open("configs/config.json", "w") as j:
                            json.dump(settings, j)
                        if self.stream is not None:
                            self.delay_time = (
                                self.stream.latency[-1]
                                + values["block_time"]
                                + values["crossfade_length"]
                                + 0.01
                            )
                        if values["I_noise_reduce"]:
                            self.delay_time += min(values["crossfade_length"], 0.04)
                        self.window["sr_stream"].update(self.gui_config.samplerate)
                        self.window["delay_time"].update(int(self.delay_time * 1000))
                # Parameter hot update
                if event == "threhold":
                    self.gui_config.threhold = values["threhold"]
                elif event == "pitch":
                    self.gui_config.pitch = values["pitch"]
                    if hasattr(self, "rvc"):
                        self.rvc.change_key(values["pitch"])
                elif event == "index_rate":
                    self.gui_config.index_rate = values["index_rate"]
                    if hasattr(self, "rvc"):
                        self.rvc.change_index_rate(values["index_rate"])
                elif event == "rms_mix_rate":
                    self.gui_config.rms_mix_rate = values["rms_mix_rate"]
                elif event in ["pm", "harvest", "crepe", "rmvpe", "fcpe"]:
                    self.gui_config.f0method = event
                elif event == "I_noise_reduce":
                    self.gui_config.I_noise_reduce = values["I_noise_reduce"]
                    if self.stream is not None:
                        self.delay_time += (
                            1 if values["I_noise_reduce"] else -1
                        ) * min(values["crossfade_length"], 0.04)
                        self.window["delay_time"].update(int(self.delay_time * 1000))
                elif event == "O_noise_reduce":
                    self.gui_config.O_noise_reduce = values["O_noise_reduce"]
                elif event == "use_pv":
                    self.gui_config.use_pv = values["use_pv"]
                elif event in ["vc", "im"]:
                    self.function = event
                elif event == "stop_vc" or event != "start_vc":
                    # Other parameters do not support hot update
                    self.stop_stream()

        def set_values(self, values):
            if len(values["pth_path"].strip()) == 0:
                sg.popup(i18n("请选择pth文件"))
                return False
            if len(values["index_path"].strip()) == 0:
                sg.popup(i18n("请选择index文件"))
                return False
            pattern = re.compile("[^\x00-\x7F]+")
            if pattern.findall(values["pth_path"]):
                sg.popup(i18n("pth文件路径不可包含中文"))
                return False
            if pattern.findall(values["index_path"]):
                sg.popup(i18n("index文件路径不可包含中文"))
                return False
            self.set_devices(values["sg_input_device"], values["sg_output_device"])
            self.config.use_jit = False  # values["use_jit"]
            # self.device_latency = values["device_latency"]
            self.gui_config.sg_input_device = values["sg_input_device"]
            self.gui_config.sg_output_device = values["sg_output_device"]
            self.gui_config.pth_path = values["pth_path"]
            self.gui_config.index_path = values["index_path"]
            self.gui_config.sr_type = ["sr_model", "sr_device"][
                [
                    values["sr_model"],
                    values["sr_device"],
                ].index(True)
            ]
            self.gui_config.threhold = values["threhold"]
            self.gui_config.pitch = values["pitch"]
            self.gui_config.block_time = values["block_time"]
            self.gui_config.crossfade_time = values["crossfade_length"]
            self.gui_config.extra_time = values["extra_time"]
            self.gui_config.I_noise_reduce = values["I_noise_reduce"]
            self.gui_config.O_noise_reduce = values["O_noise_reduce"]
            self.gui_config.use_pv = values["use_pv"]
            self.gui_config.rms_mix_rate = values["rms_mix_rate"]
            self.gui_config.index_rate = values["index_rate"]
            self.gui_config.n_cpu = values["n_cpu"]
            self.gui_config.f0method = ["pm", "harvest", "crepe", "rmvpe", "fcpe"][
                [
                    values["pm"],
                    values["harvest"],
                    values["crepe"],
                    values["rmvpe"],
                    values["fcpe"],
                ].index(True)
            ]
            return True

        def start_vc(self):
            torch.cuda.empty_cache()
            self.rvc = rvc_for_realtime.RVC(
                self.gui_config.pitch,
                self.gui_config.pth_path,
                self.gui_config.index_path,
                self.gui_config.index_rate,
                self.gui_config.n_cpu,
                inp_q,
                opt_q,
                self.config,
                self.rvc if hasattr(self, "rvc") else None,
            )
            self.gui_config.samplerate = (
                self.rvc.tgt_sr
                if self.gui_config.sr_type == "sr_model"
                else self.get_device_samplerate()
            )
            self.zc = self.gui_config.samplerate // 100
            self.block_frame = (
                int(
                    np.round(
                        self.gui_config.block_time
                        * self.gui_config.samplerate
                        / self.zc
                    )
                )
                * self.zc
            )
            self.block_frame_16k = 160 * self.block_frame // self.zc
            self.crossfade_frame = (
                int(
                    np.round(
                        self.gui_config.crossfade_time
                        * self.gui_config.samplerate
                        / self.zc
                    )
                )
                * self.zc
            )
            self.sola_buffer_frame = min(self.crossfade_frame, 4 * self.zc)
            self.sola_search_frame = self.zc
            self.extra_frame = (
                int(
                    np.round(
                        self.gui_config.extra_time
                        * self.gui_config.samplerate
                        / self.zc
                    )
                )
                * self.zc
            )
            self.input_wav: torch.Tensor = torch.zeros(
                self.extra_frame
                + self.crossfade_frame
                + self.sola_search_frame
                + self.block_frame,
                device=self.config.device,
                dtype=torch.float32,
            )
            self.input_wav_res: torch.Tensor = torch.zeros(
                160 * self.input_wav.shape[0] // self.zc,
                device=self.config.device,
                dtype=torch.float32,
            )
            self.sola_buffer: torch.Tensor = torch.zeros(
                self.sola_buffer_frame, device=self.config.device, dtype=torch.float32
            )
            self.nr_buffer: torch.Tensor = self.sola_buffer.clone()
            self.output_buffer: torch.Tensor = self.input_wav.clone()
            self.res_buffer: torch.Tensor = torch.zeros(
                2 * self.zc, device=self.config.device, dtype=torch.float32
            )
            self.skip_head = self.extra_frame // self.zc
            self.return_length = (
                self.block_frame + self.sola_buffer_frame + self.sola_search_frame
            ) // self.zc
            self.fade_in_window: torch.Tensor = (
                torch.sin(
                    0.5
                    * np.pi
                    * torch.linspace(
                        0.0,
                        1.0,
                        steps=self.sola_buffer_frame,
                        device=self.config.device,
                        dtype=torch.float32,
                    )
                )
                ** 2
            )
            self.fade_out_window: torch.Tensor = 1 - self.fade_in_window
            self.resampler = tat.Resample(
                orig_freq=self.gui_config.samplerate,
                new_freq=16000,
                dtype=torch.float32,
            ).to(self.config.device)
            if self.rvc.tgt_sr != self.gui_config.samplerate:
                self.resampler2 = tat.Resample(
                    orig_freq=self.rvc.tgt_sr,
                    new_freq=self.gui_config.samplerate,
                    dtype=torch.float32,
                ).to(self.config.device)
            else:
                self.resampler2 = None
            self.tg = TorchGate(
                sr=self.gui_config.samplerate, n_fft=4 * self.zc, prop_decrease=0.9
            ).to(self.config.device)
            self.start_stream()

        def start_stream(self):
            global flag_vc
            if not flag_vc:
                flag_vc = True
                channels = 1 if sys.platform == "darwin" else 2
                self.stream = sd.Stream(
                    channels=channels,
                    callback=self.audio_callback,
                    blocksize=self.block_frame,
                    samplerate=self.gui_config.samplerate,
                    dtype="float32",
                )
                self.stream.start()

        def stop_stream(self):
            global flag_vc
            if flag_vc:
                flag_vc = False
                if self.stream is not None:
                    self.stream.stop()
                    self.stream.close()
                    self.stream = None

        def audio_callback(
            self, indata: np.ndarray, outdata: np.ndarray, frames, times, status
        ):
            """
            音频处理
            """
            global flag_vc
            start_time = time.perf_counter()
            indata = librosa.to_mono(indata.T)
            if self.gui_config.threhold > -60:
                rms = librosa.feature.rms(
                    y=indata, frame_length=4 * self.zc, hop_length=self.zc
                )
                db_threhold = (
                    librosa.amplitude_to_db(rms, ref=1.0)[0] < self.gui_config.threhold
                )
                for i in range(db_threhold.shape[0]):
                    if db_threhold[i]:
                        indata[i * self.zc : (i + 1) * self.zc] = 0
            self.input_wav[: -self.block_frame] = self.input_wav[
                self.block_frame :
            ].clone()
            self.input_wav[-self.block_frame :] = torch.from_numpy(indata).to(
                self.config.device
            )
            self.input_wav_res[: -self.block_frame_16k] = self.input_wav_res[
                self.block_frame_16k :
            ].clone()
            # input noise reduction and resampling
            if self.gui_config.I_noise_reduce and self.function == "vc":
                input_wav = self.input_wav[
                    -self.sola_buffer_frame - self.block_frame - 2 * self.zc :
                ]
                input_wav = self.tg(
                    input_wav.unsqueeze(0), self.input_wav.unsqueeze(0)
                )[0, 2 * self.zc :]
                input_wav[: self.sola_buffer_frame] *= self.fade_in_window
                input_wav[: self.sola_buffer_frame] += (
                    self.nr_buffer * self.fade_out_window
                )
                self.nr_buffer[:] = input_wav[self.block_frame :]
                input_wav = torch.cat(
                    (self.res_buffer[:], input_wav[: self.block_frame])
                )
                self.res_buffer[:] = input_wav[-2 * self.zc :]
                self.input_wav_res[-self.block_frame_16k - 160 :] = self.resampler(
                    input_wav
                )[160:]
            else:
                self.input_wav_res[-self.block_frame_16k - 160 :] = self.resampler(
                    self.input_wav[-self.block_frame - 2 * self.zc :]
                )[160:]
            # infer
            if self.function == "vc":
                infer_wav = self.rvc.infer(
                    self.input_wav_res,
                    self.block_frame_16k,
                    self.skip_head,
                    self.return_length,
                    self.gui_config.f0method,
                )
                if self.resampler2 is not None:
                    infer_wav = self.resampler2(infer_wav)
            else:
                infer_wav = self.input_wav[
                    -self.crossfade_frame - self.sola_search_frame - self.block_frame :
                ].clone()
            # output noise reduction
            if (self.gui_config.O_noise_reduce and self.function == "vc") or (
                self.gui_config.I_noise_reduce and self.function == "im"
            ):
                self.output_buffer[: -self.block_frame] = self.output_buffer[
                    self.block_frame :
                ].clone()
                self.output_buffer[-self.block_frame :] = infer_wav[-self.block_frame :]
                infer_wav = self.tg(
                    infer_wav.unsqueeze(0), self.output_buffer.unsqueeze(0)
                ).squeeze(0)
            # volume envelop mixing
            if self.gui_config.rms_mix_rate < 1 and self.function == "vc":
                rms1 = librosa.feature.rms(
                    y=self.input_wav_res[
                        160
                        * self.skip_head : 160
                        * (self.skip_head + self.return_length)
                    ]
                    .cpu()
                    .numpy(),
                    frame_length=640,
                    hop_length=160,
                )
                rms1 = torch.from_numpy(rms1).to(self.config.device)
                rms1 = F.interpolate(
                    rms1.unsqueeze(0),
                    size=infer_wav.shape[0] + 1,
                    mode="linear",
                    align_corners=True,
                )[0, 0, :-1]
                rms2 = librosa.feature.rms(
                    y=infer_wav[:].cpu().numpy(),
                    frame_length=4 * self.zc,
                    hop_length=self.zc,
                )
                rms2 = torch.from_numpy(rms2).to(self.config.device)
                rms2 = F.interpolate(
                    rms2.unsqueeze(0),
                    size=infer_wav.shape[0] + 1,
                    mode="linear",
                    align_corners=True,
                )[0, 0, :-1]
                rms2 = torch.max(rms2, torch.zeros_like(rms2) + 1e-3)
                infer_wav *= torch.pow(
                    rms1 / rms2, torch.tensor(1 - self.gui_config.rms_mix_rate)
                )
            # SOLA algorithm from https://github.com/yxlllc/DDSP-SVC
            conv_input = infer_wav[
                None, None, : self.sola_buffer_frame + self.sola_search_frame
            ]
            cor_nom = F.conv1d(conv_input, self.sola_buffer[None, None, :])
            cor_den = torch.sqrt(
                F.conv1d(
                    conv_input**2,
                    torch.ones(1, 1, self.sola_buffer_frame, device=self.config.device),
                )
                + 1e-8
            )
            if sys.platform == "darwin":
                _, sola_offset = torch.max(cor_nom[0, 0] / cor_den[0, 0])
                sola_offset = sola_offset.item()
            else:
                sola_offset = torch.argmax(cor_nom[0, 0] / cor_den[0, 0])
            printt("sola_offset = %d", int(sola_offset))
            infer_wav = infer_wav[sola_offset:]
            if "privateuseone" in str(self.config.device) or not self.gui_config.use_pv:
                infer_wav[: self.sola_buffer_frame] *= self.fade_in_window
                infer_wav[: self.sola_buffer_frame] += (
                    self.sola_buffer * self.fade_out_window
                )
            else:
                infer_wav[: self.sola_buffer_frame] = phase_vocoder(
                    self.sola_buffer,
                    infer_wav[: self.sola_buffer_frame],
                    self.fade_out_window,
                    self.fade_in_window,
                )
            self.sola_buffer[:] = infer_wav[
                self.block_frame : self.block_frame + self.sola_buffer_frame
            ]
            if sys.platform == "darwin":
                outdata[:] = infer_wav[: self.block_frame].cpu().numpy()[:, np.newaxis]
            else:
                outdata[:] = (
                    infer_wav[: self.block_frame].repeat(2, 1).t().cpu().numpy()
                )
            total_time = time.perf_counter() - start_time
            if flag_vc:
                self.window["infer_time"].update(int(total_time * 1000))
            printt("Infer time: %.2f", total_time)

        def update_devices(self):
            """获取设备列表"""
            sd._terminate()
            sd._initialize()
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
            for hostapi in hostapis:
                for device_idx in hostapi["devices"]:
                    devices[device_idx]["hostapi_name"] = hostapi["name"]
            self.input_devices = [
                f"{d['name']} ({d['hostapi_name']})"
                for d in devices
                if d["max_input_channels"] > 0
            ]
            self.output_devices = [
                f"{d['name']} ({d['hostapi_name']})"
                for d in devices
                if d["max_output_channels"] > 0
            ]
            self.input_devices_indices = [
                d["index"] if "index" in d else d["name"]
                for d in devices
                if d["max_input_channels"] > 0
            ]
            self.output_devices_indices = [
                d["index"] if "index" in d else d["name"]
                for d in devices
                if d["max_output_channels"] > 0
            ]

        def set_devices(self, input_device, output_device):
            """设置输出设备"""
            sd.default.device[0] = self.input_devices_indices[
                self.input_devices.index(input_device)
            ]
            sd.default.device[1] = self.output_devices_indices[
                self.output_devices.index(output_device)
            ]
            printt("Input device: %s:%s", str(sd.default.device[0]), input_device)
            printt("Output device: %s:%s", str(sd.default.device[1]), output_device)

        def get_device_samplerate(self):
            return int(
                sd.query_devices(device=sd.default.device[0])["default_samplerate"]
            )

    gui = GUI()
