#pragma once
#include "hash.cu.h"
#include "hash_functions.h"

namespace tv {

namespace hash {

template <typename K, typename V, typename Hash, K EmptyKey = empty_key_v<K>,
          bool Power2 = false>
struct LinearHashTable {
public:
  using key_type_uint = to_unsigned_t<K>;
  using key_type = K;
  using value_type = pair<K, V, EmptyKey>;
  using mapped_type = V;
  using size_type = int32_t;
  // TODO no way to get constexpr uint repr of key for now.
  // static constexpr auto empty_key_uint = value_type::empty_key_uint;
  static constexpr auto empty_key = value_type::empty_key;

private:
  value_type *table_ = nullptr;
  size_type hash_size_;
  key_type_uint empty_key_uint;
  Hash hash_ftor_ = Hash();

public:
  explicit TV_HOST_DEVICE LinearHashTable(value_type *table,
                                          size_type hash_size)
      : table_(table), hash_size_(hash_size) {
    auto eky = empty_key;
    empty_key_uint = *(reinterpret_cast<const key_type_uint *>(&eky));
  }

  key_type_uint TV_DEVICE_INLINE hash(K key) const {
    key_type_uint key_u = *(reinterpret_cast<const key_type_uint *>(&key));
    return hash_ftor_(key_u);
  }

  TV_HOST_DEVICE_INLINE size_type size() const { return hash_size_; }

  TV_HOST_DEVICE_INLINE value_type *data() { return table_; }

  TV_HOST_DEVICE_INLINE const value_type *data() const { return table_; }

  void TV_DEVICE insert(const K &key, const V &value) {
    key_type_uint key_u = *(reinterpret_cast<const key_type_uint *>(&key));
    key_type_uint slot;
    if (Power2) {
      slot = hash_ftor_(key_u) & (hash_size_ - 1);
    } else {
      slot = hash_ftor_(key_u) % hash_size_;
    }
    while (true) {
      key_type_uint prev =
          atomicCAS(reinterpret_cast<key_type_uint *>(&table_[slot].first),
                    empty_key_uint, key_u);
      if (prev == empty_key_uint || prev == key_u) {
        table_[slot].second = value;
        break;
      }
      if (Power2) {
        slot = (slot + 1) & (hash_size_ - 1);
      } else {
        slot = (slot + 1) % hash_size_;
      }
    }
  }

  void TV_DEVICE insert_add(const K &key, const V &value) {
    key_type_uint key_u = *(reinterpret_cast<const key_type_uint *>(&key));
    key_type_uint slot;
    if (Power2) {
      slot = hash_ftor_(key_u) & (hash_size_ - 1);
    } else {
      slot = hash_ftor_(key_u) % hash_size_;
    }
    while (true) {
      key_type_uint prev =
          atomicCAS(reinterpret_cast<key_type_uint *>(&table_[slot].first),
                    empty_key_uint, key_u);
      if (prev == empty_key_uint || prev == key_u) {
        atomicAdd(&table_[slot].second, value);
        break;
      }
      if (Power2) {
        slot = (slot + 1) & (hash_size_ - 1);
      } else {
        slot = (slot + 1) % hash_size_;
      }
    }
  }

  value_type TV_DEVICE lookup(K key) {
    key_type_uint key_u = *(reinterpret_cast<const key_type_uint *>(&key));
    key_type_uint slot;
    if (Power2) {
      slot = hash_ftor_(key_u) & (hash_size_ - 1);
    } else {
      slot = hash_ftor_(key_u) % hash_size_;
    }
    const __restrict__ value_type *table_ptr = table_;
    while (true) {
      auto val = table_ptr[slot];
      if (val.first == key) {
        return val;
      }
      if (val.first == empty_key) {
        return {empty_key, V()};
      }
      if (Power2) {
        slot = (slot + 1) & (hash_size_ - 1);
      } else {
        slot = (slot + 1) % hash_size_;
      }
    }
    return {empty_key, V()};
  }

  value_type *TV_DEVICE lookup_ptr(K key) {
    key_type_uint key_u = *(reinterpret_cast<const key_type_uint *>(&key));
    key_type_uint slot;
    if (Power2) {
      slot = hash_ftor_(key_u) & (hash_size_ - 1);
    } else {
      slot = hash_ftor_(key_u) % hash_size_;
    }
    value_type val;
    while (true) {
      val = table_[slot];
      if (val.first == key) {
        return table_ + slot;
      }
      if (val.first == empty_key) {
        return nullptr;
      }
      if (Power2) {
        slot = (slot + 1) & (hash_size_ - 1);
      } else {
        slot = (slot + 1) % hash_size_;
      }
    }
    return nullptr;
  }

  void TV_DEVICE delete_item(K key, V empty_value) {
    key_type_uint key_u = *(reinterpret_cast<const key_type_uint *>(&key));
    key_type_uint slot;
    if (Power2) {
      slot = hash_ftor_(key_u) & (hash_size_ - 1);
    } else {
      slot = hash_ftor_(key_u) % hash_size_;
    }
    while (true) {
      if (table_[slot].first == key) {
        table_[slot].second = empty_value;
        return;
      }
      if (table_[slot].first == empty_key) {
        return;
      }
      if (Power2) {
        slot = (slot + 1) & (hash_size_ - 1);
      } else {
        slot = (slot + 1) % hash_size_;
      }
    }
    return;
  }

  void clear(cudaStream_t stream = 0) {
    auto launcher = tv::cuda::Launch(hash_size_, stream);
    launcher(clear_table<LinearHashTable>, *this);
  }

  tv::Tensor items(mapped_type empty_value, tv::Tensor out = tv::Tensor(),
                   cudaStream_t stream = nullptr) const {
    auto count = tv::zeros({1}, tv::int32, 0);
    if (out.empty()) {
      out = tv::Tensor({hash_size_}, tv::type_v<value_type>, 0);
    } else {
      TV_ASSERT_INVALID_ARG(out.device() == 0 && out.ndim() == 1 &&
                                out.dtype() == tv::type_v<value_type>,
                            "error");
    }
    auto launcher = tv::cuda::Launch(hash_size_, stream);
    auto out_tv = out.tview<value_type, 1>();
    launcher(iterate_table<LinearHashTable>, *this, out_tv,
             count.data_ptr<int32_t>(), empty_value);
    auto count_cpu = count.cpu();
    auto count_val = count_cpu.item<int32_t>();
    return out.slice_first_axis(0, count_val);
  }

  tv::Tensor items(tv::Tensor out = tv::Tensor(),
                   cudaStream_t stream = nullptr) const {
    auto count = tv::zeros({1}, tv::int32, 0);
    if (out.empty()) {
      out = tv::Tensor({hash_size_}, tv::type_v<value_type>, 0);
    } else {
      TV_ASSERT_INVALID_ARG(out.device() == 0 && out.ndim() == 1 &&
                                out.dtype() == tv::type_v<value_type>,
                            "error");
    }
    auto launcher = tv::cuda::Launch(hash_size_, stream);
    auto out_tv = out.tview<value_type, 1>();
    launcher(iterate_table_oneshot<LinearHashTable>, *this, out_tv,
             count.data_ptr<int32_t>());
    auto count_cpu = count.cpu();
    auto count_val = count_cpu.item<int32_t>();
    return out.slice_first_axis(0, count_val);
  }
};

template <typename K, typename Hash, K EmptyKey = empty_key_v<K>,
          bool Power2 = false>
struct LinearHashSet {
public:
  using key_type_uint = to_unsigned_t<K>;
  using value_type = K;
  using size_type = int32_t;
  static constexpr auto empty_key = EmptyKey;

private:
  value_type *table_ = nullptr;
  size_type hash_size_;
  Hash hash_ftor_ = Hash();
  key_type_uint empty_key_uint;

public:
  explicit TV_HOST_DEVICE LinearHashSet(value_type *table, size_type hash_size)
      : table_(table), hash_size_(hash_size) {
    auto eky = empty_key;
    empty_key_uint = *(reinterpret_cast<const key_type_uint *>(&eky));
  }

  K TV_DEVICE_INLINE hash(K key) const {
    key_type_uint key_u = *(reinterpret_cast<const key_type_uint *>(&key));
    return hash_ftor_(key_u);
  }

  TV_HOST_DEVICE_INLINE size_type size() const { return hash_size_; }

  TV_HOST_DEVICE_INLINE value_type *data() { return table_; }

  TV_HOST_DEVICE_INLINE const value_type *data() const { return table_; }

  void TV_DEVICE insert(const K &key) {
    key_type_uint key_u = *(reinterpret_cast<const key_type_uint *>(&key));
    key_type_uint slot;
    if (Power2) {
      slot = hash_ftor_(key_u) & (hash_size_ - 1);
    } else {
      slot = hash_ftor_(key_u) % hash_size_;
    }
    while (true) {
      key_type_uint prev =
          atomicCAS(reinterpret_cast<key_type_uint *>(table_ + slot),
                    empty_key_uint, key_u);
      if (prev == empty_key_uint || prev == key_u) {
        break;
      }
      if (Power2) {
        slot = (slot + 1) & (hash_size_ - 1);
      } else {
        slot = (slot + 1) % hash_size_;
      }
    }
  }

  value_type *TV_DEVICE lookup_ptr(K key) {
    key_type_uint key_u = *(reinterpret_cast<const key_type_uint *>(&key));
    key_type_uint slot;
    if (Power2) {
      slot = hash_ftor_(key_u) & (hash_size_ - 1);
    } else {
      slot = hash_ftor_(key_u) % hash_size_;
    }
    key_type_uint val;
    while (true) {
      val = table_[slot];
      if (val == key) {
        return table_ + slot;
      }
      // TODO
      if (val == empty_key) {
        return nullptr;
      }
      if (Power2) {
        slot = (slot + 1) & (hash_size_ - 1);
      } else {
        slot = (slot + 1) % hash_size_;
      }
    }
    return nullptr;
  }

  void clear(cudaStream_t stream = 0) {
    auto launcher = tv::cuda::Launch(hash_size_, stream);
    launcher(clear_set<LinearHashSet>, *this);
  }
  void clear2(cudaStream_t stream = 0) {
    auto launcher = tv::cuda::Launch(hash_size_, stream);
    launcher(clear_set2<LinearHashSet>, *this);
  }


  tv::Tensor keys(tv::Tensor out = tv::Tensor(),
                  cudaStream_t stream = nullptr) const {
    auto count = tv::zeros({1}, tv::int32, 0);
    if (out.empty()) {
      out = tv::Tensor({hash_size_}, tv::type_v<value_type>, 0);
    } else {
      TV_ASSERT_INVALID_ARG(out.device() == 0 && out.ndim() == 1 &&
                                out.dtype() == tv::type_v<value_type>,
                            "error");
    }
    auto launcher = tv::cuda::Launch(hash_size_, stream);
    auto out_tv = out.tview<value_type, 1>();
    launcher(iterate_set<LinearHashSet>, *this, out_tv,
             count.data_ptr<int32_t>());
    auto count_cpu = count.cpu();
    auto count_val = count_cpu.item<int32_t>();
    return out.slice_first_axis(0, count_val);
  }

  tv::Tensor probe_lengths() const {
    auto count = tv::zeros({1}, tv::int32, 0);
    auto out = tv::Tensor({hash_size_}, tv::int64, 0);
    auto launcher = tv::cuda::Launch(hash_size_);
    auto out_tv = out.tview<int64_t, 1>();
    launcher(set_probe_length<LinearHashSet>, *this, out_tv,
             count.data_ptr<int32_t>());
    auto count_cpu = count.cpu();
    auto count_val = count_cpu.item<int32_t>();
    return out.slice_first_axis(0, count_val);
  }

};

} // namespace hash
} // namespace tv