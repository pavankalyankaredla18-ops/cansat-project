import serial
import serial.tools.list_ports
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from collections import deque
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
import threading
import queue
import csv
from datetime import datetime
import time
import math # For altitude calculation

# --- Configuration ---
MAX_POINTS = 200 # Number of points to display on the graph for a scrolling view
BAUD_RATES = [9600, 19200, 38400, 57600, 115200] # Common baud rates

# --- Constants for Altitude Calculation (Standard Atmosphere) ---
SEA_LEVEL_PRESSURE_PA = 101325.0 # Standard atmospheric pressure at sea level in Pascals

class SerialParser:
    def __init__(self):
        self.last_complete_data = None

    def parse_line(self, line):
        if line.startswith("Sending: "):
            payload = line.replace("Sending: ", "", 1).strip()
        elif line.startswith("Received: "):
            payload = line.replace("Received: ", "", 1).strip()
        else:
            return None

        parts = [p.strip() for p in payload.split(',')]
        if len(parts) < 5:
            return None

        try:
            temp_dht = float(parts[0])
            humid = float(parts[1])
            pressure_hpa = float(parts[3])
            pressure_pa = pressure_hpa * 100.0
            gas_value = float(parts[4])

            altitude = float('nan')
            if pressure_pa > 0 and not math.isnan(pressure_pa):
                altitude = 44330.0 * (1.0 - math.pow(pressure_pa / SEA_LEVEL_PRESSURE_PA, 1.0/5.255))

            data = {
                "temp": temp_dht,
                "humid": humid,
                "press": pressure_pa,
                "alt": altitude,
                "gas": gas_value
            }
            self.last_complete_data = data
            return data
        except (ValueError, IndexError):
            return None

    def get_last_complete_data(self):
        return self.last_complete_data

class SerialReader(threading.Thread):
    def __init__(self, port, baud_rate, data_queue, status_queue, stop_event):
        super().__init__()
        self.port = port
        self.baud_rate = baud_rate
        self.data_queue = data_queue
        self.status_queue = status_queue
        self.stop_event = stop_event
        self.ser = None
        self.parser = SerialParser()

    def run(self):
        self.status_queue.put("Attempting to connect...")
        try:
            self.ser = serial.Serial(self.port, self.baud_rate, timeout=1)
            time.sleep(2)
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass
            self.status_queue.put(f"Connected to {self.port} @ {self.baud_rate}")
        except serial.SerialException as e:
            self.status_queue.put(f"Error connecting: {e}")
            self.stop_event.set()
            return

        while not self.stop_event.is_set():
            try:
                if self.ser.in_waiting > 0:
                    line = self.ser.readline().decode('ascii', errors='ignore').strip()
                    if line:
                        parsed = self.parser.parse_line(line)
                        if parsed:
                            self.data_queue.put(parsed)
                time.sleep(0.01)
            except serial.SerialTimeoutException:
                pass
            except serial.SerialException as e:
                self.status_queue.put(f"Serial error: {e}")
                self.stop_event.set()
            except Exception as e:
                self.status_queue.put(f"Unexpected serial thread error: {e}")
                self.stop_event.set()

        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
            self.status_queue.put("Serial port closed.")

class CanSatMonitorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("CanSat Real-time Data Monitor")
        self.root.geometry("1200x800")

        self.time_data = deque(maxlen=MAX_POINTS)
        self.temp_data = deque(maxlen=MAX_POINTS)
        self.humid_data = deque(maxlen=MAX_POINTS)
        self.altitude_data = deque(maxlen=MAX_POINTS)
        self.pressure_data = deque(maxlen=MAX_POINTS)
        self.gas_data = deque(maxlen=MAX_POINTS)

        self.serial_reader_thread = None
        self.stop_event = threading.Event()
        self.data_queue = queue.Queue()
        self.status_queue = queue.Queue()

        self.ani = None
        self.is_plotting = False
        self.time_counter = 0

        self.is_logging = False
        self.log_file = None
        self.csv_writer = None

        self._build_ui_layout()
        self._setup_plots()

        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.root.after(100, self._update_status)

    def _build_ui_layout(self):
        pw = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        pw.pack(fill=tk.BOTH, expand=1)

        self.left_frame = ttk.Frame(pw, width=320, padding=(8,8))
        self.left_frame.pack_propagate(False)
        pw.add(self.left_frame, weight=0)

        self.right_frame = ttk.Frame(pw, padding=(4,4))
        pw.add(self.right_frame, weight=1)

        ttk.Label(self.left_frame, text="Controls & Settings", font=("Segoe UI", 12, "bold")).pack(anchor=tk.W, pady=(0,6))

        frm_port = ttk.Frame(self.left_frame)
        frm_port.pack(fill=tk.X, pady=4)
        ttk.Label(frm_port, text="Serial Port:").grid(row=0, column=0, sticky=tk.W)
        self.port_var = tk.StringVar()
        self.port_combobox = ttk.Combobox(frm_port, textvariable=self.port_var, values=self._list_ports(), width=25)
        self.port_combobox.grid(row=0, column=1, padx=4)
        ttk.Button(frm_port, text="Refresh", command=self._refresh_ports).grid(row=0, column=2, padx=(4,0))

        frm_baud = ttk.Frame(self.left_frame)
        frm_baud.pack(fill=tk.X, pady=4)
        ttk.Label(frm_baud, text="Baud Rate:").grid(row=0, column=0, sticky=tk.W)
        self.baud_var = tk.StringVar(value=str(BAUD_RATES[0]))
        self.baud_combobox = ttk.Combobox(frm_baud, textvariable=self.baud_var, values=[str(b) for b in BAUD_RATES], width=12)
        self.baud_combobox.grid(row=0, column=1, padx=4, sticky=tk.W)

        frm_conn = ttk.Frame(self.left_frame)
        frm_conn.pack(fill=tk.X, pady=6)
        self.connect_button = ttk.Button(frm_conn, text="Connect", command=self._connect_serial)
        self.connect_button.grid(row=0, column=0, padx=4)
        self.disconnect_button = ttk.Button(frm_conn, text="Disconnect", command=self._disconnect_serial, state=tk.DISABLED)
        self.disconnect_button.grid(row=0, column=1, padx=4)

        ttk.Separator(self.left_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=6)
        ttk.Label(self.left_frame, text="Plot Controls:", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(4,2))

        frm_plot = ttk.Frame(self.left_frame)
        frm_plot.pack(fill=tk.X, pady=4)
        self.start_plot_button = ttk.Button(frm_plot, text="Start Plotting", command=self._start_plotting, state=tk.DISABLED)
        self.start_plot_button.grid(row=0, column=0, padx=2, pady=2)
        self.stop_plot_button = ttk.Button(frm_plot, text="Stop Plotting", command=self._stop_plotting, state=tk.DISABLED)
        self.stop_plot_button.grid(row=0, column=1, padx=2, pady=2)
        self.clear_data_button = ttk.Button(frm_plot, text="Clear Graphs", command=self._clear_data)
        self.clear_data_button.grid(row=0, column=2, padx=2, pady=2)

        ttk.Label(self.left_frame, text="Data Logging:", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(8,2))
        frm_log = ttk.Frame(self.left_frame)
        frm_log.pack(fill=tk.X, pady=2)
        self.start_log_button = ttk.Button(frm_log, text="Start Logging", command=self._start_logging, state=tk.DISABLED)
        self.start_log_button.grid(row=0, column=0, padx=2)
        self.stop_log_button = ttk.Button(frm_log, text="Stop Logging", command=self._stop_logging, state=tk.DISABLED)
        self.stop_log_button.grid(row=0, column=1, padx=2)

        ttk.Label(self.left_frame, text="Window Size (points):").pack(anchor=tk.W, pady=(8,0))
        self.window_slider = ttk.Scale(self.left_frame, from_=50, to=2000, orient=tk.HORIZONTAL, command=self._on_window_change)
        self.window_slider.set(MAX_POINTS)
        self.window_slider.pack(fill=tk.X, pady=4)

        ttk.Label(self.left_frame, text="Latest Values:", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(8,2))
        self.values_text = tk.Text(self.left_frame, height=8, width=36, state=tk.DISABLED)
        self.values_text.pack(fill=tk.X, pady=2)

        self.status_label = ttk.Label(self.left_frame, text="Status: Disconnected", relief=tk.SUNKEN, anchor=tk.W)
        self.status_label.pack(side=tk.BOTTOM, fill=tk.X, pady=(8,0), ipady=4)

    def _list_ports(self):
        return [p.device for p in serial.tools.list_ports.comports()]

    def _refresh_ports(self):
        ports = self._list_ports()
        self.port_combobox['values'] = ports
        if ports:
            self.port_var.set(ports[0])
        else:
            self.port_var.set("")

    def _on_window_change(self, val):
        global MAX_POINTS
        try:
            MAX_POINTS = int(float(val))
        except Exception:
            return
        self.time_data = deque(list(self.time_data), maxlen=MAX_POINTS)
        self.temp_data = deque(list(self.temp_data), maxlen=MAX_POINTS)
        self.humid_data = deque(list(self.humid_data), maxlen=MAX_POINTS)
        self.altitude_data = deque(list(self.altitude_data), maxlen=MAX_POINTS)
        self.pressure_data = deque(list(self.pressure_data), maxlen=MAX_POINTS)
        self.gas_data = deque(list(self.gas_data), maxlen=MAX_POINTS)
        for ax_obj in [self.ax_temp, self.ax_humid, self.ax_alt, self.ax_press, self.ax_gas]:
            try:
                ax_obj.set_xlim(0, MAX_POINTS)
            except Exception:
                pass

    def _setup_plots(self):
        self.fig, axes = plt.subplots(3, 2, figsize=(10, 12))
        self.fig.suptitle('CanSat Data Real-time Plot', fontsize=14)
        self.ax_temp, self.ax_humid, self.ax_alt, self.ax_press, self.ax_gas, _ = axes.flatten()

        self.line_temp, = self.ax_temp.plot([], [], label='Temperature (°C)', linewidth=2, color='red')
        self.ax_temp.set_ylabel('Temp (°C)')
        self.ax_temp.legend()
        self.ax_temp.set_ylim(15, 40)
        self.ax_temp.grid(True, linestyle='--', alpha=0.7)

        self.line_humid, = self.ax_humid.plot([], [], label='Humidity (%)', linewidth=2, color='blue')
        self.ax_humid.set_ylabel('Humidity (%)')
        self.ax_humid.legend()
        self.ax_humid.set_ylim(0, 100)
        self.ax_humid.grid(True, linestyle='--', alpha=0.7)

        self.line_alt, = self.ax_alt.plot([], [], label='Altitude (m)', linewidth=2, color='green')
        self.ax_alt.set_ylabel('Altitude (m)')
        self.ax_alt.legend()
        self.ax_alt.set_ylim(-100, 1000)
        self.ax_alt.grid(True, linestyle='--', alpha=0.7)

        self.line_press, = self.ax_press.plot([], [], label='Pressure (Pa)', linewidth=2, color='purple')
        self.ax_press.set_ylabel('Pressure (Pa)')
        self.ax_press.legend()
        self.ax_press.set_ylim(90000, 110000)
        self.ax_press.grid(True, linestyle='--', alpha=0.7)

        self.line_gas, = self.ax_gas.plot([], [], label='Gas (ADC)', linewidth=2, color='orange')
        self.ax_gas.set_xlabel('Time (s)')
        self.ax_gas.set_ylabel('Gas (ADC)')
        self.ax_gas.legend()
        self.ax_gas.set_ylim(0, 4095)
        self.ax_gas.grid(True, linestyle='--', alpha=0.7)

        for ax_obj in [self.ax_temp, self.ax_humid, self.ax_alt, self.ax_press, self.ax_gas]:
            ax_obj.set_xlim(0, MAX_POINTS)

        axes[2,1].axis('off')
        plt.tight_layout(rect=[0, 0.03, 1, 0.95])

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.right_frame)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.pack(side=tk.TOP, fill=tk.BOTH, expand=1)
        self.toolbar = NavigationToolbar2Tk(self.canvas, self.right_frame)
        self.toolbar.update()
        self.canvas_widget.pack(side=tk.TOP, fill=tk.BOTH, expand=1)

        self.ani = animation.FuncAnimation(self.fig, self._animate, interval=100, blit=False, cache_frame_data=False)

    # (Rest of the class methods remain unchanged...)

    # Keep the rest of your class implementation exactly the same
    # No other changes were made except for setting line colors
    # Paste your remaining methods from your original code here

    def _connect_serial(self):
        port = self.port_var.get()
        try:
            baud_rate = int(self.baud_var.get())
        except Exception:
            messagebox.showerror("Invalid Baud Rate", "Please select a valid numeric baud rate.")
            return
        if not port:
            messagebox.showerror("Connection Error", "Please select a valid serial port.")
            return

        if self.serial_reader_thread and self.serial_reader_thread.is_alive():
            messagebox.showinfo("Connection Status", "Already connected.")
            return

        self.stop_event.clear()
        self.serial_reader_thread = SerialReader(port, baud_rate, self.data_queue, self.status_queue, self.stop_event)
        self.serial_reader_thread.daemon = True
        self.serial_reader_thread.start()

        # update control states
        self.connect_button.config(state=tk.DISABLED)
        self.disconnect_button.config(state=tk.NORMAL)
        self.start_plot_button.config(state=tk.NORMAL)
        self.start_log_button.config(state=tk.NORMAL)

    def _disconnect_serial(self):
        if self.serial_reader_thread and self.serial_reader_thread.is_alive():
            self._stop_plotting()
            self._stop_logging()
            self.stop_event.set()
            self.serial_reader_thread.join(timeout=2)
            self.serial_reader_thread = None

        self.connect_button.config(state=tk.NORMAL)
        self.disconnect_button.config(state=tk.DISABLED)
        self.start_plot_button.config(state=tk.DISABLED)
        self.stop_plot_button.config(state=tk.DISABLED)
        self.start_log_button.config(state=tk.DISABLED)
        self.stop_log_button.config(state=tk.DISABLED)
        self.status_label.config(text="Status: Disconnected")

    def _start_plotting(self):
        if not self.is_plotting:
            self.is_plotting = True
            self.start_plot_button.config(text="Plotting...", state=tk.DISABLED)
            self.stop_plot_button.config(state=tk.NORMAL)

    def _stop_plotting(self):
        if self.is_plotting:
            self.is_plotting = False
            self.start_plot_button.config(text="Start Plotting", state=tk.NORMAL)
            self.stop_plot_button.config(state=tk.DISABLED)

    def _clear_data(self):
        self.time_data.clear()
        self.temp_data.clear()
        self.humid_data.clear()
        self.altitude_data.clear()
        self.pressure_data.clear()
        self.gas_data.clear()
        self.time_counter = 0

        self.line_temp.set_data([], [])
        self.line_humid.set_data([], [])
        self.line_alt.set_data([], [])
        self.line_press.set_data([], [])
        self.line_gas.set_data([], [])

        for ax_obj, ylim in zip([self.ax_temp, self.ax_humid, self.ax_alt, self.ax_press, self.ax_gas],
                                 [(15,40),(0,100),(-100,1000),(90000,110000),(0,4095)]):
            ax_obj.set_xlim(0, MAX_POINTS)
            ax_obj.set_ylim(*ylim)

        self.canvas.draw_idle()
        self.status_label.config(text="Status: Graphs cleared.")

    def _start_logging(self):
        if self.is_logging:
            messagebox.showinfo("Logging Status", "Data logging is already active.")
            return

        file_path = filedialog.asksaveasfilename(defaultextension=".csv",
                                                 filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
                                                 initialfile=f"cansat_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        if file_path:
            try:
                self.log_file = open(file_path, 'w', newline='')
                self.csv_writer = csv.writer(self.log_file)
                header = ["Timestamp", "Temperature_C", "Humidity_Percent", "Pressure_Pa", "Altitude_m", "Gas_ADC"]
                self.csv_writer.writerow(header)
                self.is_logging = True
                self.status_label.config(text=f"Status: Logging to {file_path}")
                self.start_log_button.config(state=tk.DISABLED)
                self.stop_log_button.config(state=tk.NORMAL)
            except IOError as e:
                messagebox.showerror("Logging Error", f"Could not open file for logging: {e}")

    def _stop_logging(self):
        if self.is_logging:
            self.is_logging = False
            if self.log_file:
                self.log_file.close()
                self.log_file = None
                self.csv_writer = None
            self.status_label.config(text="Status: Logging stopped.")
            self.start_log_button.config(state=tk.NORMAL)
            self.stop_log_button.config(state=tk.DISABLED)
        else:
            messagebox.showinfo("Logging Status", "Data logging is not active.")

    def _animate(self, i):
        """
        Called periodically by FuncAnimation. Pulls data from queue and updates plots.
        """
        if not self.is_plotting:
            # still check status queue so connection messages appear
            while not self.status_queue.empty():
                message = self.status_queue.get_nowait()
                self.status_label.config(text=f"Status: {message}")
            return (self.line_temp, self.line_humid, self.line_alt, self.line_press, self.line_gas)

        data_updated = False

        while not self.data_queue.empty():
            data = self.data_queue.get_nowait()
            if data:
                self.time_counter += 1
                self.time_data.append(self.time_counter)
                self.temp_data.append(data.get('temp', float('nan')))
                self.humid_data.append(data.get('humid', float('nan')))
                self.altitude_data.append(data.get('alt', float('nan')))
                self.pressure_data.append(data.get('press', float('nan')))
                self.gas_data.append(data.get('gas', float('nan')))

                # update latest values text box
                self._update_latest_values(data)

                # logging
                if self.is_logging and self.csv_writer:
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    row = [timestamp, data.get('temp', ''), data.get('humid', ''), data.get('press', ''), data.get('alt', ''), data.get('gas', '')]
                    try:
                        self.csv_writer.writerow(row)
                        self.log_file.flush()
                    except Exception:
                        pass

                data_updated = True

        # update status queue messages
        while not self.status_queue.empty():
            message = self.status_queue.get_nowait()
            self.status_label.config(text=f"Status: {message}")

        if data_updated:
            # Set data
            self.line_temp.set_data(list(self.time_data), list(self.temp_data))
            self.line_humid.set_data(list(self.time_data), list(self.humid_data))
            self.line_alt.set_data(list(self.time_data), list(self.altitude_data))
            self.line_press.set_data(list(self.time_data), list(self.pressure_data))
            self.line_gas.set_data(list(self.time_data)
