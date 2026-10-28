import test from 'node:test';
import assert from 'node:assert/strict';
import dgram from 'node:dgram';
import { EventEmitter } from 'node:events';
import net from 'node:net';
import {
  BackendError,
  createDiagnostics,
  disposeStation,
  executeRequest,
  errorResponse,
  installEgressGuards,
  isPrivateIPv4,
  makeOfflineApi,
  normalizeInput,
  primePeerOnListen,
  normalizeStationDeviceType,
  sanitizeEventRecords,
  setMotionAndVerify,
  udpDestination,
} from '../src/local-backend.mjs';
import { CommandName, DeviceType, Station } from 'eufy-security-client';

function request(overrides = {}) {
  const stationSN = 'station-fixture';
  const deviceSN = 'doorbell-fixture';
  return {
    version: 1,
    operation: 'events.local',
    role: 'guest',
    station: {
      raw: {
        station_sn: stationSN,
        station_name: 'fixture',
        station_model: 'T8010',
        device_type: 10002,
        p2p_did: 'fixture-p2p-id',
        main_sw_version: '3.4.2.6h',
        params: [],
        member: { admin_user_id: 'fixture-account' },
        devices: [{ device_sn: deviceSN, device_type: DeviceType.BATTERY_DOORBELL, device_channel: 0 }],
      },
      local: { host: '10.254.50.20', dskKey: 'fixture-cached-key', dskExpirationMs: Date.now() + 600_000 },
    },
    device: {
      device_sn: deviceSN,
      device_name: 'fixture doorbell',
      device_model: 'T8210',
      device_type: DeviceType.BATTERY_DOORBELL,
      device_channel: 0,
      station_sn: stationSN,
      main_sw_version: '2.2.4.6',
      params: [],
    },
    query: { startDate: '2026-10-01', endDate: '2026-10-12', maxItems: 20 },
    ...overrides,
  };
}

test('private IPv4 check accepts only RFC1918 addresses', () => {
  assert.equal(isPrivateIPv4('10.0.0.8'), true);
  assert.equal(isPrivateIPv4('172.16.0.8'), true);
  assert.equal(isPrivateIPv4('172.31.255.254'), true);
  assert.equal(isPrivateIPv4('10.254.50.20'), true);
  assert.equal(isPrivateIPv4('172.32.0.1'), false);
  assert.equal(isPrivateIPv4('127.0.0.1'), false);
  assert.equal(isPrivateIPv4('8.8.8.8'), false);
  assert.equal(isPrivateIPv4('fe80::1'), false);
});

test('T8010 archive station type is normalized only for that exact model', () => {
  assert.equal(normalizeStationDeviceType(10002, 'T8010'), DeviceType.STATION);
  assert.throws(() => normalizeStationDeviceType(10002, 'other'), (error) => error.code === 'UNSUPPORTED_STATION');
  assert.equal(normalizeStationDeviceType(DeviceType.HB3, 'T8030'), DeviceType.HB3);
});

test('normalized T8010 record advertises upstream local event query support', async () => {
  const station = await Station.getInstance({ isConnected: () => false }, {
    station_sn: 'station-fixture',
    station_name: 'fixture',
    station_model: 'T8010',
    device_type: normalizeStationDeviceType(10002, 'T8010'),
    p2p_did: 'fixture-p2p-id',
    app_conn: '',
    main_sw_version: '3.4.2.6h',
    params: [],
    member: { admin_user_id: 'fixture-account' },
    devices: [],
  }, '10.254.50.20');
  try {
    station.initialize();
    assert.equal(station.hasCommand(CommandName.StationDatabaseQueryLocal), true);
    assert.equal(station.isStation(), true);
  } finally {
    station.p2pSession.socket.removeAllListeners();
    try { station.p2pSession.socket.close(); } catch { /* unbound fixture socket */ }
  }
});

test('missing and expired DSK metadata stays absent without blocking LAN-only normalization', () => {
  const expired = request();
  expired.station.local.dskKey = 'must-not-forward-this-secret';
  expired.station.local.dskExpirationMs = 1_700_000_000_000;
  const normalizedExpired = normalizeInput(expired);
  assert.equal(normalizedExpired.dskKey, '');
  assert.equal(normalizedExpired.dskExpirationMs, 1_700_000_000_000);

  const missing = request();
  missing.station.local.dskKey = null;
  missing.station.local.dskExpirationMs = null;
  const normalizedMissing = normalizeInput(missing);
  assert.equal(normalizedMissing.dskKey, '');
  assert.equal(normalizedMissing.dskExpirationMs, null);

  const rendered = JSON.stringify({ normalizedExpired, normalizedMissing });
  assert.equal(rendered.includes('must-not-forward-this-secret'), false);
});

test('offline API disables cloud refresh and returns undefined cipher for SDK Level-1 fallback', async () => {
  const diagnostics = createDiagnostics();
  const api = makeOfflineApi(diagnostics);
  assert.equal(api.isConnected(), false);
  assert.equal(await api.getCipher(12, 'fixture-account'), undefined);
  assert.equal(diagnostics.gatewayFallbackRequests, 1);
  assert.throws(() => api.request({ endpoint: 'must-not-be-used' }), (error) => error.code === 'CLOUD_DISABLED');
});

test('motion writes without explicit Admin access fail before socket creation', async () => {
  const write = request({ operation: 'motion.set' });
  await assert.rejects(executeRequest(write), (error) => error instanceof BackendError && error.code === 'WRITE_ROLE_REQUIRED');
});

test('non-private target and mismatched device are rejected before local session setup', async () => {
  const publicHost = request();
  publicHost.station.local.host = '8.8.8.8';
  await assert.rejects(executeRequest(publicHost), (error) => error.code === 'INVALID_LOCAL_ADDRESS');

  const wrongDevice = request();
  wrongDevice.device.station_sn = 'another-station';
  await assert.rejects(executeRequest(wrongDevice), (error) => error.code === 'INVALID_REQUEST');
});

test('UDP send guard blocks every destination except the pinned station IP', async () => {
  const guard = installEgressGuards('10.254.50.20');
  try {
    const fakeSocket = Object.create(dgram.Socket.prototype);
    const error = await new Promise((resolve) => {
      fakeSocket.send(Buffer.from('test'), 0, 4, 32108, '10.254.50.21', (sendError) => resolve(sendError));
    });
    assert.match(error.message, /blocked/);
    assert.deepEqual(guard.denied, ['10.254.50.21']);
    assert.deepEqual(udpDestination([Buffer.from('x'), 32108, '10.254.50.20']), { host: '10.254.50.20', port: 32108 });
  } finally {
    guard.restore();
  }
});

test('TCP networking guard throws without opening a connection', () => {
  const guard = installEgressGuards('10.254.50.20');
  try {
    assert.throws(() => net.connect(32108, '10.254.50.20'), (error) => error.code === 'NETWORK_BLOCKED');
  } finally {
    guard.restore();
  }
});

test('station cleanup clears SDK timers and closes its socket without reconnecting', async () => {
  let timerFired = false;
  const sdkTimer = setTimeout(() => { timerFired = true; }, 20);
  const reconnectTimer = setTimeout(() => { timerFired = true; }, 20);
  const socket = {
    closed: false,
    removeAllListeners() {},
    close() { this.closed = true; },
  };
  const station = {
    terminating: false,
    reconnectTimeout: reconnectTimer,
    p2pSession: {
      terminating: false,
      lookupTimeout: sdkTimer,
      currentMessageState: { data: { waitForAudioData: sdkTimer } },
      messageStates: new Map(),
      messageVideoStates: new Map(),
      isConnected: () => false,
      socket,
    },
  };
  await disposeStation(station);
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(timerFired, false);
  assert.equal(socket.closed, true);
  assert.equal(station.terminating, true);
});

test('event sanitizer exposes timestamps and frame counts, not identifiers or paths', () => {
  const result = sanitizeEventRecords([{
    record_id: 'secret-event-id',
    device_sn: 'secret-device-id',
    cloud_path: '/private/path',
    history: {
      start_time: new Date('2026-10-12T11:00:00Z'),
      end_time: new Date('2026-10-12T11:00:15Z'),
      frame_num: '5',
      storage_path: '/private/video',
    },
  }]);
  assert.deepEqual(result, [{
    startTime: '2026-10-12T11:00:00.000Z',
    endTime: '2026-10-12T11:00:15.000Z',
    frameCount: 5,
  }]);
  assert.equal(JSON.stringify(result).includes('secret'), false);
  assert.equal(JSON.stringify(result).includes('/private'), false);
});

test('invalid event history input is rejected rather than becoming a false empty result', () => {
  assert.throws(() => sanitizeEventRecords(null), (error) => error.code === 'INVALID_LOCAL_RESPONSE');
});

test('diagnostics retain only bounded protocol metadata and omit secret values and identifiers', () => {
  const diagnostics = createDiagnostics();
  diagnostics.packet(Buffer.from([0xf1, 0x41, 0xaa, 0xbb]));
  diagnostics.command({ command_type: 1103, channel: 0, return_code: -148, customData: { secret: 'do-not-copy' } });
  diagnostics.secondaryCommand({ command_type: 1306, channel: 0, return_code: 0 });
  diagnostics.cameraInfo({
    secret_top_level: 'hidden',
    params: [{ dev_type: 0, param_type: 1011, param_value: 'secret-motion-value' }],
    db_bypass_str: [{ channel: 0, param_type: 1011, param_value: 'secret-base64' }],
  });
  diagnostics.rawProperty({ 1011: { value: 'secret-property-value', source: 'p2p' } });
  const result = diagnostics.snapshot({ p2pSession: { encryption: 1, p2pKey: Buffer.from([1]) } }, 0);
  const json = JSON.stringify(result);

  assert.deepEqual(result.packets, [{ header: 'f141', length: 4, count: 1 }]);
  assert.deepEqual(result.commands, [{ commandType: 1103, channel: 0, returnCode: -148 }]);
  assert.deepEqual(result.cameraInfo[0].keys, ['params', 'db_bypass_str']);
  assert.deepEqual(result.cameraInfo[0].paramTypes, [1011]);
  assert.deepEqual(result.cameraInfo[0].dbBypassChannels, [0]);
  assert.deepEqual(result.rawProperties[0], { propertyTypes: [1011], sources: ['p2p'], valueTypes: ['string'] });
  assert.equal(result.encryptionLevel, 1);
  assert.equal(result.encryptionKeyPresent, true);
  assert.equal(json.includes('secret'), false);
  assert.equal(json.includes('fixture'), false);
});

test('motion setting requires a fresh original, matching ACK and independent fresh readback', async () => {
  const { EventEmitter } = await import('node:events');
  const { setMotionAndVerify } = await import('../src/local-backend.mjs');
  const station = new EventEmitter();
  let enabled = true;
  let reads = 0;
  let writes = 0;
  station.isConnected = () => true;
  const device = request().device;
  station.getCameraInfo = () => {
    reads += 1;
    queueMicrotask(() => station.emit('raw device property changed', device.device_sn, { 1011: { value: enabled ? '1' : '0', source: 'p2p' } }));
  };
  station.setMotionDetection = (_camera, value) => {
    writes += 1;
    enabled = value;
    queueMicrotask(() => {
      station.emit('command result', station, { channel: 99, return_code: -148, customData: { property: { name: 'motionDetection' } } });
      station.emit('command result', station, { channel: 0, return_code: 0, customData: { property: { name: 'motionDetection' } } });
    });
  };
  const result = await setMotionAndVerify(station, device, makeOfflineApi(), false, Date.now() + 1000);
  assert.equal(result.previous, true);
  assert.equal(result.enabled, false);
  assert.equal(result.fresh, true);
  assert.equal(reads, 2);
  assert.equal(writes, 1);
  const same = await setMotionAndVerify(station, device, makeOfflineApi(), false, Date.now() + 1000);
  assert.equal(same.changed, false);
  assert.equal(writes, 1);
  assert.equal(station.listenerCount('command result'), 0);
  assert.equal(station.listenerCount('raw device property changed'), 0);
});


test('identity-matched peer port is optional and invalid values fail before sockets', async () => {
  const legacy = request();
  assert.equal(normalizeInput(legacy).peerPort, undefined);
  legacy.station.local.peerPort = 25000;
  assert.equal(normalizeInput(legacy).peerPort, 25000);
  for (const port of [0, -1, 65536, 1.5, '25000', true, null]) {
    legacy.station.local.peerPort = port;
    await assert.rejects(executeRequest(legacy), (error) => error.code === 'INVALID_REQUEST');
  }
});

test('peer flow is primed once on the SDK socket before lookup and keeps the pinned-host guard', () => {
  const sent = [];
  const originalSend = dgram.Socket.prototype.send;
  dgram.Socket.prototype.send = function (...args) { sent.push(args); };
  const guard = installEgressGuards('10.254.50.20');
  try {
    const socket = new EventEmitter();
    socket.send = dgram.Socket.prototype.send;
    primePeerOnListen(socket, '10.254.50.20', 25000);
    socket.once('listening', () => socket.send(Buffer.from('f13000020000', 'hex'), 32108, '10.254.50.20'));
    assert.equal(sent.length, 0);
    socket.emit('listening');
    assert.deepEqual(sent.map((args) => [args[0].length, args[1], args[2]]), [[0, 25000, '10.254.50.20'], [6, 32108, '10.254.50.20']]);
    socket.emit('listening');
    assert.equal(sent.length, 2);
    primePeerOnListen(socket, '10.254.50.21', 25000);
    socket.emit('listening');
    assert.equal(guard.denied.length, 1);
    assert.equal(sent.length, 2);
    assert.equal(socket.listenerCount('listening'), 0);
  } finally {
    guard.restore();
    dgram.Socket.prototype.send = originalSend;
  }
});

test('write attempt tracking distinguishes original-read failure from a possible setter send', async () => {
  const device = request().device;
  const station = new EventEmitter();
  station.isConnected = () => true;
  const state = { attempted: false };
  station.getCameraInfo = () => {};
  station.setMotionDetection = () => { throw new Error('possible send before SDK failure'); };
  await assert.rejects(
    setMotionAndVerify(station, device, makeOfflineApi(), false, Date.now() + 20, state),
    (error) => error.code === 'PROPERTY_UNAVAILABLE',
  );
  assert.equal(state.attempted, false);
  station.getCameraInfo = () => queueMicrotask(() => station.emit('raw device property changed', device.device_sn, { 1011: { value: '1', source: 'p2p' } }));
  await assert.rejects(
    setMotionAndVerify(station, device, makeOfflineApi(), false, Date.now() + 1000, state),
    (error) => error.code === 'LOCAL_COMMAND_FAILED',
  );
  assert.equal(state.attempted, true);
  assert.equal(station.listenerCount('command result'), 0);
});

test('readback mismatch and missing ACK retain write-attempt state and clear listeners', async () => {
  const device = request().device;
  for (const ack of [true, false]) {
    const station = new EventEmitter();
    station.isConnected = () => true;
    station.getCameraInfo = () => queueMicrotask(() => station.emit('raw device property changed', device.device_sn, { 1011: { value: '1', source: 'p2p' } }));
    let writes = 0;
    station.setMotionDetection = () => {
      writes += 1;
      if (ack) queueMicrotask(() => station.emit('command result', station, { channel: 0, return_code: 0, customData: { property: { name: 'motionDetection' } } }));
    };
    const state = { attempted: false };
    await assert.rejects(
      setMotionAndVerify(station, device, makeOfflineApi(), false, Date.now() + 50, state),
      (error) => error.code === (ack ? 'WRITE_READBACK_MISMATCH' : 'WRITE_ACK_TIMEOUT'),
    );
    assert.equal(state.attempted, true);
    assert.equal(writes, 1);
    assert.equal(station.listenerCount('command result'), 0);
    assert.equal(station.listenerCount('raw device property changed'), 0);
  }
});

test('bridge error carries explicit safe-retry proof only when provided by the operation', () => {
  const error = new BackendError('LOCAL_TIMEOUT', 'connection timed out');
  assert.equal(errorResponse(error).error.details?.retrySafe, undefined);
  error.retrySafe = false;
  assert.equal(errorResponse(error).error.details.retrySafe, false);
  error.retrySafe = true;
  assert.equal(errorResponse(error).error.details.retrySafe, true);
});
