import { fileURLToPath } from 'node:url';
import net from 'node:net';
import http from 'node:http';
import https from 'node:https';
import dgram from 'node:dgram';
import {
  BatteryDoorbellCamera,
  CommandName,
  DatabaseReturnCode,
  DeviceType,
  FilterDetectType,
  FilterEventType,
  FilterStorageType,
  P2PConnectionType,
  PropertyName,
  Station,
} from 'eufy-security-client';

const OPERATION_BUDGET_MS = 13_000;
const CLEANUP_BUDGET_MS = 1_500;
const MAX_STDIN_BYTES = 1_000_000;
const MAX_HISTORY_ITEMS = 20;
const MAX_DIAGNOSTIC_ITEMS = 32;
const MAX_DIAGNOSTIC_PACKETS = 256;

export class BackendError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'BackendError';
    this.code = code;
  }
}

function fail(code, message) {
  throw new BackendError(code, message);
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function isPrivateIPv4(value) {
  if (typeof value !== 'string' || net.isIP(value) !== 4) return false;
  const octets = value.split('.').map(Number);
  return octets[0] === 10 ||
    (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
    (octets[0] === 192 && octets[1] === 168);
}

function safeString(value, field, maxLength = 256) {
  if (typeof value !== 'string' || value.length === 0 || value.length > maxLength || value.includes('\0')) {
    fail('INVALID_REQUEST', `Invalid ${field}.`);
  }
  return value;
}

function safeInteger(value, field, min = 0, max = 0x7fffffff) {
  if (!Number.isInteger(value) || value < min || value > max) {
    fail('INVALID_REQUEST', `Invalid ${field}.`);
  }
  return value;
}

export function normalizeStationDeviceType(deviceType, stationModel) {
  if (deviceType === 10002 && stationModel === 'T8010') return DeviceType.STATION;
  if (Station.isStation(deviceType)) return deviceType;
  fail('UNSUPPORTED_STATION', 'The cached station model is not supported by this local backend.');
}

function safeParams(value) {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    if (!isPlainObject(entry) || !Number.isInteger(entry.param_type)) return [];
    const paramValue = entry.param_value;
    if (typeof paramValue !== 'string' && typeof paramValue !== 'number' && typeof paramValue !== 'boolean') return [];
    return [{ param_type: entry.param_type, param_value: String(paramValue) }];
  }).slice(0, 256);
}

export function normalizeInput(input) {
  if (!isPlainObject(input) || input.version !== 1) {
    fail('INVALID_REQUEST', 'Expected a version 1 JSON request.');
  }
  const operation = input.operation;
  if (!['motion.get', 'events.local', 'events.counts', 'motion.set'].includes(operation)) {
    fail('UNSUPPORTED_OPERATION', 'Unsupported local operation.');
  }
  if (input.role !== 'guest' && input.role !== 'admin' && input.role !== 'unknown') {
    fail('INVALID_REQUEST', 'Invalid cached app role.');
  }
  if (!isPlainObject(input.station) || !isPlainObject(input.station.raw) || !isPlainObject(input.station.local)) {
    fail('INVALID_REQUEST', 'Station metadata is incomplete.');
  }
  const stationInput = input.station.raw;
  const localInput = input.station.local;
  const stationSN = safeString(stationInput.station_sn, 'station serial', 128);
  const cachedStationType = safeInteger(stationInput.device_type, 'station type');
  const stationModel = typeof stationInput.station_model === 'string' ? stationInput.station_model : '';
  const stationType = normalizeStationDeviceType(cachedStationType, stationModel);
  const p2pDid = safeString(stationInput.p2p_did, 'P2P device identity', 256);
  const host = safeString(localInput.host, 'local station address', 64);
  if (!isPrivateIPv4(host)) fail('INVALID_LOCAL_ADDRESS', 'The station address must be a private IPv4 address.');
  // This adapter never forwards the app's cached DSK. The SDK can still try
  // its local lookup; its DSK refresh path is disabled by the offline API.
  const dskKey = '';
  const peerPort = localInput.peerPort === undefined ? undefined : safeInteger(localInput.peerPort, 'local peer port', 1, 65535);
  const dskExpirationMs = localInput.dskExpirationMs === undefined || localInput.dskExpirationMs === null
    ? null
    : localInput.dskExpirationMs;
  if (dskExpirationMs !== null && (!Number.isSafeInteger(dskExpirationMs) || dskExpirationMs < 0)) {
    fail('INVALID_REQUEST', 'Invalid cached local session expiration.');
  }
  if (!isPlainObject(stationInput.member) || typeof stationInput.member.admin_user_id !== 'string' || stationInput.member.admin_user_id.length === 0) {
    fail('INVALID_REQUEST', 'Station member metadata is incomplete.');
  }
  if (!isPlainObject(input.device)) fail('INVALID_REQUEST', 'Device metadata is incomplete.');

  const deviceInput = input.device;
  const deviceSN = safeString(deviceInput.device_sn, 'device serial', 128);
  const deviceType = safeInteger(deviceInput.device_type, 'device type');
  const deviceChannel = safeInteger(deviceInput.device_channel, 'device channel', 0, 65535);
  if (deviceInput.station_sn !== stationSN) fail('INVALID_REQUEST', 'Device is not attached to the supplied station.');
  if (!Array.isArray(stationInput.devices) || !stationInput.devices.some((entry) =>
    isPlainObject(entry) && entry.device_sn === deviceSN && entry.device_type === deviceType && entry.device_channel === deviceChannel
  )) {
    fail('INVALID_REQUEST', 'The target device is not listed on the supplied station.');
  }

  if (operation === 'motion.set') {
    if (input.role !== 'admin') fail('WRITE_ROLE_REQUIRED', 'Motion changes require explicit Admin access.');
    if (typeof input.value !== 'boolean') fail('INVALID_REQUEST', 'Motion value must be boolean.');
    if (deviceType !== DeviceType.BATTERY_DOORBELL || deviceInput.device_model !== 'T8210' || stationModel !== 'T8010') {
      fail('UNSUPPORTED_DEVICE', 'Motion writes are limited to the validated T8210 and T8010 pair.');
    }
  }

  let query;
  if (operation === 'events.local' || operation === 'events.counts') {
    if (!isPlainObject(input.query)) fail('INVALID_REQUEST', 'A date range is required for local event history.');
    const startDate = parseLocalDate(input.query.startDate, 'start date');
    const endDate = parseLocalDate(input.query.endDate, 'end date');
    const days = Math.floor((Date.UTC(endDate.getFullYear(), endDate.getMonth(), endDate.getDate()) - Date.UTC(startDate.getFullYear(), startDate.getMonth(), startDate.getDate())) / 86_400_000);
    if (days < 0 || days > 30) fail('INVALID_DATE_RANGE', 'The local event date range must be within 31 calendar days.');
    const requestedItems = input.query.maxItems === undefined ? MAX_HISTORY_ITEMS : input.query.maxItems;
    if (!Number.isInteger(requestedItems) || requestedItems < 1 || requestedItems > MAX_HISTORY_ITEMS) {
      fail('INVALID_REQUEST', 'maxItems must be between 1 and 20.');
    }
    query = { startDate, endDate, maxItems: requestedItems };
  }

  const member = { admin_user_id: stationInput.member.admin_user_id };
  const safeDevice = {
    device_sn: deviceSN,
    device_name: typeof deviceInput.device_name === 'string' ? deviceInput.device_name : '',
    device_model: typeof deviceInput.device_model === 'string' ? deviceInput.device_model : '',
    device_type: deviceType,
    device_channel: deviceChannel,
    station_sn: stationSN,
    main_sw_version: typeof deviceInput.main_sw_version === 'string' ? deviceInput.main_sw_version : '',
    main_hw_version: typeof deviceInput.main_hw_version === 'string' ? deviceInput.main_hw_version : '',
    params: safeParams(deviceInput.params),
  };
  const safeStation = {
    station_sn: stationSN,
    station_name: typeof stationInput.station_name === 'string' ? stationInput.station_name : '',
    station_model: stationModel,
    device_type: stationType,
    p2p_did: p2pDid,
    app_conn: '',
    ip_addr: host,
    main_sw_version: typeof stationInput.main_sw_version === 'string' ? stationInput.main_sw_version : '',
    main_hw_version: typeof stationInput.main_hw_version === 'string' ? stationInput.main_hw_version : '',
    time_zone: typeof stationInput.time_zone === 'string' ? stationInput.time_zone : '',
    params: safeParams(stationInput.params),
    member,
    devices: [{
      device_sn: deviceSN,
      device_type: deviceType,
      device_channel: deviceChannel,
    }],
  };
  return { operation, role: input.role, host, peerPort, dskKey, dskExpirationMs, station: safeStation, device: safeDevice, query, value: input.value };
}

function parseLocalDate(value, field) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    fail('INVALID_REQUEST', `Invalid ${field}.`);
  }
  const [year, month, day] = value.split('-').map(Number);
  const date = new Date(year, month - 1, day);
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) {
    fail('INVALID_REQUEST', `Invalid ${field}.`);
  }
  return date;
}

export function udpDestination(args) {
  if (args.length >= 5 && Number.isInteger(args[3]) && typeof args[4] === 'string') {
    return { host: args[4], port: args[3] };
  }
  if (args.length >= 3 && Number.isInteger(args[1]) && typeof args[2] === 'string') {
    return { host: args[2], port: args[1] };
  }
  const options = args.find((value) => isPlainObject(value) && Number.isInteger(value.port));
  if (options) return { host: options.address, port: options.port };
  return null;
}

export function sanitizeEventRecords(records, limit = MAX_HISTORY_ITEMS) {
  if (!Array.isArray(records)) fail('INVALID_LOCAL_RESPONSE', 'Local event history response was invalid.');
  return records.flatMap((record) => {
    if (!isPlainObject(record) || !isPlainObject(record.history)) return [];
    const history = record.history;
    const asISO = (value) => value instanceof Date && Number.isFinite(value.getTime()) ? value.toISOString() : null;
    const frameCount = history.frame_num !== null && history.frame_num !== undefined && history.frame_num !== '' && Number.isFinite(Number(history.frame_num))
      ? Number(history.frame_num)
      : null;
    return [{
      startTime: asISO(history.start_time),
      endTime: asISO(history.end_time),
      frameCount,
    }];
  }).slice(0, limit);
}

export function sanitizeDailyCounts(records, query) {
  if (!Array.isArray(records) || records.length > 31) fail('INVALID_LOCAL_RESPONSE', 'Daily counts response was invalid.');
  const seen = new Set();
  const counts = records.map((record) => {
    const day = record?.day;
    if (!(day instanceof Date) || !Number.isFinite(day.getTime()) || !Number.isSafeInteger(record.count) || record.count < 0) {
      fail('INVALID_LOCAL_RESPONSE', 'Daily counts response was invalid.');
    }
    // The SDK parses the reported calendar date as local midnight. Preserve
    // that date rather than shifting it through a UTC conversion.
    const date = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`;
    if (day < query.startDate || day > query.endDate || seen.has(date)) fail('INVALID_LOCAL_RESPONSE', 'Daily counts response was invalid.');
    seen.add(date);
    return { date, count: record.count };
  });
  return counts.sort((a, b) => b.date.localeCompare(a.date));
}

export function makeOfflineApi(diagnostics) {
  const noCloud = () => fail('CLOUD_DISABLED', 'Cloud access is disabled in local-only mode.');
  return {
    isConnected: () => false,
    // The pinned SDK awaits getCipher() for gateway info. Undefined selects
    // its built-in Level-1 fallback without consulting a cloud service.
    getCipher: async () => {
      if (diagnostics) diagnostics.gatewayFallbackRequests += 1;
      return undefined;
    },
    request: noCloud,
    getPublicKey: noCloud,
    getVoices: noCloud,
    refreshStationData: noCloud,
    getDevices: noCloud,
  };
}

export function createDiagnostics() {
  const packetCounts = new Map();
  const commands = [];
  const secondaryCommands = [];
  const cameraInfo = [];
  const rawProperties = [];
  let packetTotal = 0;

  const recordCommand = (target, result) => {
    if (!isPlainObject(result) || target.length >= MAX_DIAGNOSTIC_ITEMS) return;
    target.push({
      commandType: Number.isInteger(result.command_type) ? result.command_type : null,
      channel: Number.isInteger(result.channel) ? result.channel : null,
      returnCode: Number.isInteger(result.return_code) ? result.return_code : null,
    });
  };
  const numericIds = (entries, field) => [...new Set((Array.isArray(entries) ? entries : [])
    .map((entry) => isPlainObject(entry) ? entry[field] : undefined)
    .filter((value) => Number.isSafeInteger(value) && value >= 0 && value <= 65535))]
    .slice(0, MAX_DIAGNOSTIC_ITEMS);

  return {
    gatewayFallbackRequests: 0,
    packet(message) {
      if (!Buffer.isBuffer(message) || packetTotal >= MAX_DIAGNOSTIC_PACKETS) return;
      packetTotal += 1;
      const header = message.length >= 2 ? message.subarray(0, 2).toString('hex') : 'short';
      const key = `${header}:${message.length}`;
      if (packetCounts.has(key)) packetCounts.set(key, packetCounts.get(key) + 1);
      else if (packetCounts.size < MAX_DIAGNOSTIC_ITEMS) packetCounts.set(key, 1);
    },
    command(result) { recordCommand(commands, result); },
    secondaryCommand(result) { recordCommand(secondaryCommands, result); },
    cameraInfo(info) {
      if (!isPlainObject(info) || cameraInfo.length >= MAX_DIAGNOSTIC_ITEMS) return;
      const params = Array.isArray(info.params) ? info.params : [];
      const bypass = Array.isArray(info.db_bypass_str) ? info.db_bypass_str : [];
      cameraInfo.push({
        keys: ['params', 'db_bypass_str', 'main_sw_version', 'sec_sw_version'].filter((key) => Object.hasOwn(info, key)),
        paramCount: params.length,
        paramTypes: numericIds(params, 'param_type'),
        parameterChannels: numericIds(params, 'dev_type'),
        dbBypassCount: bypass.length,
        dbBypassParamTypes: numericIds(bypass, 'param_type'),
        dbBypassChannels: numericIds(bypass, 'channel'),
      });
    },
    rawProperty(rawValues) {
      if (!isPlainObject(rawValues) || rawProperties.length >= MAX_DIAGNOSTIC_ITEMS) return;
      const propertyTypes = [];
      const sources = [];
      const valueTypes = [];
      for (const [key, item] of Object.entries(rawValues)) {
        if (/^\d{1,5}$/.test(key)) propertyTypes.push(Number(key));
        if (!isPlainObject(item)) continue;
        if (['p2p', 'http', 'push', 'mqtt'].includes(item.source)) sources.push(item.source);
        if (Object.hasOwn(item, 'value')) valueTypes.push(item.value === null ? 'null' : typeof item.value);
      }
      rawProperties.push({
        propertyTypes: [...new Set(propertyTypes)].slice(0, MAX_DIAGNOSTIC_ITEMS),
        sources: [...new Set(sources)],
        valueTypes: [...new Set(valueTypes)],
      });
    },
    snapshot(station, blockedCount = 0) {
      const session = station?.p2pSession;
      return {
        packets: [...packetCounts].map(([key, count]) => {
          const [header, length] = key.split(':');
          return { header, length: Number(length), count };
        }),
        packetTotal,
        commands,
        secondaryCommands,
        cameraInfo,
        rawProperties,
        gatewayFallbackRequests: this.gatewayFallbackRequests,
        encryptionLevel: Number.isInteger(session?.encryption) ? session.encryption : null,
        encryptionKeyPresent: Buffer.isBuffer(session?.p2pKey) && session.p2pKey.length > 0,
        blockedNetworkAttempts: blockedCount,
      };
    },
  };
}

function attachDiagnostics(station, diagnostics) {
  const onCommand = (_sourceStation, result) => diagnostics.command(result);
  const onSecondaryCommand = (_sourceStation, result) => diagnostics.secondaryCommand(result);
  const onCameraInfo = (info) => diagnostics.cameraInfo(info);
  const onRawProperty = (_deviceSerial, properties) => diagnostics.rawProperty(properties);
  station.on('command result', onCommand);
  station.on('secondary command result', onSecondaryCommand);
  station.on('raw device property changed', onRawProperty);
  station.p2pSession.on('camera info', onCameraInfo);
  return () => {
    station.off('command result', onCommand);
    station.off('secondary command result', onSecondaryCommand);
    station.off('raw device property changed', onRawProperty);
    station.p2pSession.off('camera info', onCameraInfo);
  };
}

export function installEgressGuards(host) {
  const originalSend = dgram.Socket.prototype.send;
  const originalTcpConnect = net.Socket.prototype.connect;
  const originalNetConnect = net.connect;
  const originalHttpRequest = http.request;
  const originalHttpGet = http.get;
  const originalHttpsRequest = https.request;
  const originalHttpsGet = https.get;
  const originalFetch = globalThis.fetch;
  const denied = [];
  const blockTcp = () => fail('NETWORK_BLOCKED', 'TCP and HTTP networking are disabled in local-only mode.');

  dgram.Socket.prototype.send = function guardedSend(...args) {
    const destination = udpDestination(args);
    if (destination && destination.host === host && Number.isInteger(destination.port) && destination.port >= 1 && destination.port <= 65535) {
      return originalSend.apply(this, args);
    }
    denied.push(destination?.host ?? 'unknown');
    const error = new Error('UDP destination blocked by local-only policy.');
    const callback = [...args].reverse().find((value) => typeof value === 'function');
    if (callback) {
      process.nextTick(callback, error);
      return this;
    }
    process.nextTick(() => this.emit('error', error));
    return this;
  };
  net.Socket.prototype.connect = blockTcp;
  net.connect = blockTcp;
  http.request = blockTcp;
  http.get = blockTcp;
  https.request = blockTcp;
  https.get = blockTcp;
  globalThis.fetch = blockTcp;

  return {
    denied,
    restore() {
      dgram.Socket.prototype.send = originalSend;
      net.Socket.prototype.connect = originalTcpConnect;
      net.connect = originalNetConnect;
      http.request = originalHttpRequest;
      http.get = originalHttpGet;
      https.request = originalHttpsRequest;
      https.get = originalHttpsGet;
      globalThis.fetch = originalFetch;
    },
  };
}

function bindSocketToHost(socket, host, diagnostics) {
  const originalEmit = socket.emit;
  socket.emit = function hostFilteredEmit(event, ...args) {
    if (event === 'message' && args[1]?.address !== host) return true;
    return originalEmit.call(this, event, ...args);
  };
  socket.on('message', (message) => diagnostics.packet(message));
}

function getRemainingBudget(deadline, timeoutCode, timeoutMessage) {
  const remaining = deadline - Date.now();
  if (remaining <= 0) fail(timeoutCode, timeoutMessage);
  return remaining;
}

function waitForStationConnect(station, deadline) {
  if (station.isConnected()) return Promise.resolve(station);
  return new Promise((resolve, reject) => {
    let settled = false;
    let duration;
    try {
      duration = getRemainingBudget(deadline, 'LOCAL_TIMEOUT', 'Timed out waiting for a local station connection.');
    } catch (error) {
      reject(error);
      return;
    }
    const timer = setTimeout(() => finish(new BackendError('LOCAL_TIMEOUT', 'Timed out waiting for a local station connection.')), duration);
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      station.off('connect', onConnect);
      station.off('connection error', onError);
      if (error) reject(error);
      else resolve(value);
    };
    const onConnect = (connectedStation) => finish(null, connectedStation);
    const onError = () => finish(new BackendError('LOCAL_CONNECT_FAILED', 'Could not establish a local station connection.'));
    station.once('connect', onConnect);
    station.once('connection error', onError);
    Promise.resolve(station.connect()).catch(() => finish(new BackendError('LOCAL_CONNECT_FAILED', 'Could not establish a local station connection.')));
  });
}

export async function disposeStation(station) {
  if (!station) return;
  station.terminating = true;
  if (station.reconnectTimeout) clearTimeout(station.reconnectTimeout);
  station.reconnectTimeout = undefined;
  const session = station.p2pSession;
  if (!session) return;
  session.terminating = true;
  if (session.isConnected()) {
    let cleanupTimer;
    const closePromise = Promise.resolve().then(() => session.close()).catch(() => undefined);
    await Promise.race([
      closePromise,
      new Promise((resolve) => { cleanupTimer = setTimeout(resolve, CLEANUP_BUDGET_MS); }),
    ]);
    if (cleanupTimer) clearTimeout(cleanupTimer);
  }
  clearSessionTimers(session);
  const socket = session.socket;
  if (socket) {
    socket.removeAllListeners();
    try {
      socket.close();
    } catch {
      // A socket that never bound, or already closed, needs no further action.
    }
  }
}

function clearTimerFields(value) {
  if (!value || typeof value !== 'object') return;
  for (const [key, child] of Object.entries(value)) {
    if (/timeout|waitfor/i.test(key) && child !== undefined && child !== null) clearTimeout(child);
  }
}

function clearSessionTimers(session) {
  clearTimerFields(session);
  for (const state of Object.values(session.currentMessageState ?? {})) clearTimerFields(state);
  for (const map of [session.messageStates, session.messageVideoStates]) {
    if (map && typeof map.values === 'function') {
      for (const state of map.values()) clearTimerFields(state);
    }
  }
}

export function primePeerOnListen(socket, host, peerPort) {
  if (peerPort === undefined) return;
  if (!isPrivateIPv4(host)) fail('INVALID_LOCAL_ADDRESS', 'The station address must be a private IPv4 address.');
  safeInteger(peerPort, 'local peer port', 1, 65535);
  // Local lookup replies originate from the station's P2P port, not 32108.
  // Establish that outbound UDP flow before the SDK's normal lookup, so
  // stateful host firewalls accept the reply without a firewall rule.
  socket.once('listening', () => socket.send(Buffer.alloc(0), peerPort, host, () => {}));
}

async function createLocalStation(request, api, diagnostics) {
  const station = await Station.getInstance(api, request.station, request.host);
  station.initialize();
  station.setConnectionType(P2PConnectionType.ONLY_LOCAL);
  station.p2pSession.setConnectionType(P2PConnectionType.ONLY_LOCAL);
  station.p2pSession.cloudAddresses = [];
  station.p2pSession.dskKey = request.dskKey;
  station.p2pSession.dskExpiration = request.dskExpirationMs === null ? null : new Date(request.dskExpirationMs);
  bindSocketToHost(station.p2pSession.socket, request.host, diagnostics);
  primePeerOnListen(station.p2pSession.socket, request.host, request.peerPort);
  return station;
}

function isReadOnlyCameraType(deviceType) {
  return [DeviceType.BATTERY_DOORBELL, DeviceType.BATTERY_DOORBELL_2].includes(deviceType);
}

async function getFreshMotion(station, deviceRecord, api, deadline) {
  if (!isReadOnlyCameraType(deviceRecord.device_type)) {
    fail('UNSUPPORTED_DEVICE', 'Fresh motion status is not supported for this camera type.');
  }
  const camera = new BatteryDoorbellCamera(api, deviceRecord, {}, { simultaneousDetections: true });
  camera.initialize();
  const metadata = camera.getPropertyMetadata(PropertyName.DeviceMotionDetection);
  if (!camera.hasProperty(PropertyName.DeviceMotionDetection)) {
    fail('UNSUPPORTED_DEVICE', 'Fresh motion status is unavailable for this camera.');
  }
  await waitForStationConnect(station, deadline);
  const resultPromise = new Promise((resolve, reject) => {
    let duration;
    try {
      duration = getRemainingBudget(deadline, 'LOCAL_TIMEOUT', 'Timed out waiting for a local motion status.');
    } catch (error) {
      reject(error);
      return;
    }
    const timer = setTimeout(() => finish(new BackendError('PROPERTY_UNAVAILABLE', 'The station did not return a fresh motion status.')), duration);
    const finish = (error, result) => {
      clearTimeout(timer);
      station.off('raw device property changed', onRawProperty);
      if (error) reject(error);
      else resolve(result);
    };
    const onRawProperty = (deviceSN, rawProperties) => {
      if (deviceSN !== deviceRecord.device_sn || !isPlainObject(rawProperties)) return;
      const update = rawProperties[String(metadata.key)];
      if (!isPlainObject(update) || update.source !== 'p2p' || !Object.hasOwn(update, 'value')) return;
      try {
        camera.updateRawProperties({ [metadata.key]: update });
        const value = camera.getPropertyValue(PropertyName.DeviceMotionDetection);
        if (typeof value !== 'boolean') {
          finish(new BackendError('INVALID_LOCAL_RESPONSE', 'The station returned an invalid motion status.'));
          return;
        }
        finish(null, { enabled: value, fresh: true, source: 'local-p2p' });
      } catch {
        finish(new BackendError('INVALID_LOCAL_RESPONSE', 'The station returned an invalid motion status.'));
      }
    };
    station.on('raw device property changed', onRawProperty);
    try {
      station.getCameraInfo();
    } catch {
      finish(new BackendError('LOCAL_COMMAND_FAILED', 'Could not request fresh motion status.'));
    }
  });
  return resultPromise;
}

export async function setMotionAndVerify(station, deviceRecord, api, value, deadline, writeState = {}) {
  const before = await getFreshMotion(station, deviceRecord, api, deadline);
  if (before.enabled === value) return { ...before, previous: before.enabled, changed: false };
  const camera = new BatteryDoorbellCamera(api, deviceRecord, {}, { simultaneousDetections: true });
  camera.initialize();
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new BackendError('WRITE_ACK_TIMEOUT', 'Motion command acknowledgement timed out; state may have changed.')), getRemainingBudget(deadline, 'LOCAL_TIMEOUT', 'Motion request timed out.'));
    const finish = (error) => {
      clearTimeout(timer);
      station.off('command result', onResult);
      if (error) reject(error); else resolve();
    };
    const onResult = (_station, result) => {
      if (result?.customData?.property?.name !== PropertyName.DeviceMotionDetection || result.channel !== deviceRecord.device_channel) return;
      if (result.return_code !== 0) {
        const error = new BackendError('LOCAL_COMMAND_FAILED', 'Motion setting was rejected.');
        error.returnCode = result.return_code;
        finish(error);
      } else finish();
    };
    station.on('command result', onResult);
    try {
      // Mark before invoking the SDK: even a synchronous failure may have sent.
      writeState.attempted = true;
      station.setMotionDetection(camera, value);
    }
    catch { finish(new BackendError('LOCAL_COMMAND_FAILED', 'Could not send the motion setting.')); }
  });
  let after;
  try { after = await getFreshMotion(station, deviceRecord, api, deadline); }
  catch { fail('WRITE_READBACK_UNAVAILABLE', 'Motion was acknowledged but fresh readback failed; check status before assuming its state.'); }
  if (after.enabled !== value) fail('WRITE_READBACK_MISMATCH', 'Fresh motion state does not match the requested setting.');
  return { ...after, previous: before.enabled, changed: true };
}

export async function getLocalEvents(station, deviceRecord, query, deadline, countsOnly = false) {
  const commandName = countsOnly ? CommandName.StationDatabaseCountByDate : CommandName.StationDatabaseQueryLocal;
  const eventName = countsOnly ? 'database count by date' : 'database query local';
  if (!station.hasCommand(commandName)) {
    fail('UNSUPPORTED_OPERATION', 'The station does not expose local event history.');
  }
  await waitForStationConnect(station, deadline);
  return new Promise((resolve, reject) => {
    let commandAcknowledged = false;
    let queryReceived = false;
    let records;
    let settled = false;
    let duration;
    try {
      duration = getRemainingBudget(deadline, 'LOCAL_TIMEOUT', 'Timed out waiting for local event history.');
    } catch (error) {
      reject(error);
      return;
    }
    const timer = setTimeout(() => finish(new BackendError('LOCAL_TIMEOUT', 'Timed out waiting for local event history.')), duration);
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      station.off('command result', onCommandResult);
      station.off(eventName, onQueryResult);
      if (error) reject(error);
      else resolve(value);
    };
    const onCommandResult = (_sourceStation, result) => {
      if (result?.customData?.command?.name !== commandName) return;
      if (result.return_code !== 0) {
        const rejection = new BackendError('LOCAL_COMMAND_FAILED', 'The local event-history command was not acknowledged.');
        rejection.returnCode = Number.isInteger(result.return_code) ? result.return_code : null;
        finish(rejection);
        return;
      }
      commandAcknowledged = true;
      maybeFinish();
    };
    const onQueryResult = (_sourceStation, returnCode, data) => {
      if (returnCode !== DatabaseReturnCode.SUCCESSFUL || !Array.isArray(data)) {
        finish(new BackendError('LOCAL_QUERY_FAILED', 'The station rejected the local event-history query.'));
        return;
      }
      queryReceived = true;
      records = data;
      maybeFinish();
    };
    const maybeFinish = () => {
      if (!commandAcknowledged || !queryReceived) return;
      try {
        if (countsOnly) {
          finish(null, { counts: sanitizeDailyCounts(records, query), scope: 'homebase', complete: false, source: 'local-p2p' });
        } else {
          const events = sanitizeEventRecords(records, query.maxItems);
          finish(null, { events, count: events.length, source: 'local-p2p' });
        }
      } catch {
        finish(new BackendError('INVALID_LOCAL_RESPONSE', 'The local event-history response was invalid.'));
      }
    };
    station.on('command result', onCommandResult);
    station.on(eventName, onQueryResult);
    try {
      if (countsOnly) {
        station.databaseCountByDate(query.startDate, query.endDate);
      } else station.databaseQueryLocal(
        [deviceRecord.device_sn],
        query.startDate,
        query.endDate,
        FilterEventType.ALL,
        FilterDetectType.NOT_SUPPORT,
        FilterStorageType.LOCAL,
      );
    } catch {
      finish(new BackendError('LOCAL_QUERY_FAILED', 'Could not request local event history.'));
    }
  });
}

export async function executeRequest(input) {
  const request = normalizeInput(input);
  const deadline = Date.now() + OPERATION_BUDGET_MS;
  const diagnostics = createDiagnostics();
  const api = makeOfflineApi(diagnostics);
  const egress = installEgressGuards(request.host);
  let station;
  const writeState = { attempted: false };
  let detachDiagnostics = () => {};
  try {
    station = await createLocalStation(request, api, diagnostics);
    detachDiagnostics = attachDiagnostics(station, diagnostics);
    let result;
    if (request.operation === 'motion.set') {
      result = await setMotionAndVerify(station, request.device, api, request.value, deadline, writeState);
    } else if (request.operation === 'motion.get') {
      result = await getFreshMotion(station, request.device, api, deadline);
    } else {
      result = await getLocalEvents(station, request.device, request.query, deadline, request.operation === 'events.counts');
    }
    if (egress.denied.length > 0) fail('NETWORK_BLOCKED', 'A non-local network destination was blocked.');
    return { ok: true, result, diagnostics: diagnostics.snapshot(station, egress.denied.length) };
  } catch (error) {
    const backendError = error instanceof BackendError
      ? error
      : new BackendError('LOCAL_OPERATION_FAILED', 'The local read-only operation failed.');
    if (error instanceof BackendError) {
      backendError.localConnected = Boolean(station?.isConnected());
    }
    backendError.retrySafe = !writeState.attempted;
    backendError.diagnostics = diagnostics.snapshot(station, egress.denied.length);
    throw backendError;
  } finally {
    detachDiagnostics();
    await disposeStation(station);
    egress.restore();
  }
}

export function errorResponse(error) {
  const code = error instanceof BackendError ? error.code : 'LOCAL_OPERATION_FAILED';
  const message = error instanceof BackendError ? error.message : 'The local operation failed.';
  const details = {};
  if (typeof error?.retrySafe === 'boolean') details.retrySafe = error.retrySafe;
  if (typeof error?.localConnected === 'boolean') details.localConnected = error.localConnected;
  if (Number.isInteger(error?.returnCode)) details.returnCode = error.returnCode;
  if (error?.diagnostics && isPlainObject(error.diagnostics)) details.diagnostics = error.diagnostics;
  return { ok: false, error: { code, message, ...(Object.keys(details).length ? { details } : {}) } };
}

async function readStdinJson() {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > MAX_STDIN_BYTES) fail('INVALID_REQUEST', 'Request JSON exceeds the size limit.');
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    fail('INVALID_REQUEST', 'Request is not valid JSON.');
  }
}

async function main() {
  let output;
  try {
    const input = await readStdinJson();
    output = await executeRequest(input);
  } catch (error) {
    output = errorResponse(error);
  }
  process.stdout.write(`${JSON.stringify(output)}\n`);
}

if (process.argv[1] && process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch(() => {
    process.stdout.write(`${JSON.stringify(errorResponse(new BackendError('LOCAL_OPERATION_FAILED', 'The local operation failed.')))}\n`);
  });
}
