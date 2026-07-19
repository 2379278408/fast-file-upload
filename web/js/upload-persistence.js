const DATABASE_NAME = 'personal-transfer-timeline';
const DATABASE_VERSION = 1;
const STORE_NAME = 'upload-tasks';
const PERSISTED_FIELDS = [
  'uploadId', 'clientRequestId', 'fileHandle', 'identity', 'name', 'sizeBytes',
  'mimeType', 'status', 'confirmedParts', 'confirmedBytes', 'sourceDeviceId',
  'isSourceDevice', 'errorCode', 'errorMessage', 'createdAt',
  'serverSequence', 'serverUpdatedAt', 'serverVersion',
];

function closedError() {
  const error = new Error('Upload persistence is closed');
  error.name = 'ClosedError';
  return error;
}

export function createUploadPersistence({ indexedDB }) {
  let databasePromise = null;
  let database = null;
  let closed = false;
  const pendingRejects = new Set();

  const open = () => {
    if (closed) return Promise.reject(closedError());
    if (databasePromise) return databasePromise;
    databasePromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.onupgradeneeded = () => {
        const database = request.result;
        if (!database.objectStoreNames.contains(STORE_NAME)) {
          database.createObjectStore(STORE_NAME, { keyPath: 'uploadId' });
        }
      };
      request.onsuccess = () => {
        if (closed) {
          request.result.close();
          reject(closedError());
          return;
        }
        database = request.result;
        resolve(database);
      };
      request.onerror = () => reject(request.error || new Error('IndexedDB open failed'));
    });
    return databasePromise;
  };

  const transact = async (mode, operation) => {
    if (closed) throw closedError();
    const openedDatabase = await open();
    if (closed) throw closedError();
    return new Promise((resolve, reject) => {
      let settled = false;
      let result;
      let requestError = null;
      const transaction = openedDatabase.transaction(STORE_NAME, mode);
      const finish = callback => value => {
        if (settled) return;
        settled = true;
        pendingRejects.delete(rejectClosed);
        callback(value);
      };
      const resolveOnce = finish(resolve);
      const rejectOnce = finish(reject);
      const rejectClosed = () => rejectOnce(closedError());
      pendingRejects.add(rejectClosed);
      transaction.oncomplete = () => {
        if (closed) rejectClosed();
        else resolveOnce(result);
      };
      const rejectTransaction = () => rejectOnce(
        transaction.error || requestError || new Error('IndexedDB transaction failed'),
      );
      transaction.onabort = rejectTransaction;
      transaction.onerror = rejectTransaction;
      try {
        const request = operation(transaction.objectStore(STORE_NAME));
        request.onsuccess = () => { result = request.result; };
        request.onerror = () => {
          requestError = request.error || new Error('IndexedDB request failed');
        };
      } catch (error) {
        rejectOnce(error);
      }
    });
  };

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
        return await transact('readwrite', store => store.put(record));
      } catch (error) {
        if (closed || error.name !== 'DataCloneError'
            || !Object.prototype.hasOwnProperty.call(record, 'fileHandle')) throw error;
        delete record.fileHandle;
        return transact('readwrite', store => store.put(record));
      }
    },
    async getAll() {
      return transact('readonly', store => store.getAll());
    },
    async remove(uploadId) {
      return transact('readwrite', store => store.delete(uploadId));
    },
    close() {
      if (closed) return;
      closed = true;
      Array.from(pendingRejects).forEach(reject => reject());
      pendingRejects.clear();
      if (database) database.close();
    },
  };
}
