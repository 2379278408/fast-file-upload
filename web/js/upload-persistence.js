const DATABASE_NAME = 'personal-transfer-timeline';
const DATABASE_VERSION = 1;
const STORE_NAME = 'upload-tasks';
const PERSISTED_FIELDS = [
  'uploadId', 'clientRequestId', 'fileHandle', 'identity', 'name', 'sizeBytes',
  'mimeType', 'status', 'confirmedParts', 'confirmedBytes', 'sourceDeviceId',
  'isSourceDevice', 'errorCode', 'errorMessage', 'createdAt',
];

function requestPromise(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('IndexedDB request failed'));
  });
}

export function createUploadPersistence({ indexedDB }) {
  let databasePromise;
  const open = () => {
    if (databasePromise) return databasePromise;
    databasePromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.onupgradeneeded = () => {
        const database = request.result;
        if (!database.objectStoreNames.contains(STORE_NAME)) {
          database.createObjectStore(STORE_NAME, { keyPath: 'uploadId' });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('IndexedDB open failed'));
    });
    return databasePromise;
  };
  const store = async mode => (await open()).transaction(STORE_NAME, mode).objectStore(STORE_NAME);

  return {
    async put(task) {
      const record = {};
      PERSISTED_FIELDS.forEach(field => {
        if (field !== 'fileHandle' && task[field] !== undefined) record[field] = task[field];
      });
      if (task.fileHandle !== undefined && task.fileHandle !== null) {
        record.fileHandle = task.fileHandle;
      }
      try {
        return await requestPromise((await store('readwrite')).put(record));
      } catch (error) {
        if (!Object.prototype.hasOwnProperty.call(record, 'fileHandle')) throw error;
        delete record.fileHandle;
        return requestPromise((await store('readwrite')).put(record));
      }
    },
    async getAll() {
      return requestPromise((await store('readonly')).getAll());
    },
    async remove(uploadId) {
      return requestPromise((await store('readwrite')).delete(uploadId));
    },
    close() {
      if (databasePromise) databasePromise.then(database => database.close());
      databasePromise = null;
    },
  };
}
