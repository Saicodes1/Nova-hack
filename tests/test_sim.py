from node.hardware import SIM_PEAK_W, Hardware


def test_simulated_jetson_readings_are_labelled(monkeypatch):
    monkeypatch.setenv("NODE_SIM", "jetson")
    hardware = Hardware()
    idle_power, idle_temp = hardware.read()
    assert hardware.source == "simulated"
    assert 3.0 <= idle_power <= 4.0 and 41 <= idle_temp <= 43
    hardware._sim_at -= 5  # five seconds of load
    busy_power, _ = hardware.read(busy=True)
    assert idle_power < busy_power <= SIM_PEAK_W


def test_no_simulation_unless_asked(monkeypatch):
    monkeypatch.setenv("NODE_SIM", "0")
    monkeypatch.setattr("node.hardware.shutil.which", lambda _: None)
    hardware = Hardware()
    hardware.read()
    assert hardware.source == "none" and hardware.power_w is None
